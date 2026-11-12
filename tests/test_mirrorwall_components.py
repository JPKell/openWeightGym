"""Image model parts told apart from their headers, what a profile still needs, and early reference refusals."""
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from mirrorwall import model
from mirrorwall.components import check, identify
from mirrorwall.front import Front
from mirrorwall.runtime import native_command


def gguf(path, tensors, metadata=None):
    """A GGUF header naming tensors with PyTorch-order shapes, and no weights."""
    metadata=metadata or {}
    data = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(metadata))
    for key,value in metadata.items():
        key,value=key.encode(),value.encode()
        data+=struct.pack('<Q',len(key))+key+struct.pack('<IQ',8,len(value))+value
    for name, shape in tensors.items():
        encoded = name.encode()
        data += struct.pack("<Q", len(encoded)) + encoded + struct.pack("<I", len(shape))
        data += b"".join(struct.pack("<Q", one) for one in reversed(shape)) + struct.pack("<IQ", 0, 0)
    path.write_bytes(data)
    return str(path)


def safetensors(path, tensors):
    """A safetensors header naming tensors and shapes, and no weights."""
    header = json.dumps({name: {"dtype": "F16", "shape": list(shape), "data_offsets": [0, 0]} for name, shape in tensors.items()}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    return str(path)


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "image"
    folder.mkdir()
    return {"folder": folder,
            "klein9": gguf(folder / "klein-9b.gguf", {"model.diffusion_model.img_in.weight": (4096, 128), "model.diffusion_model.double_blocks.0.img_attn.proj.weight": (4096, 4096)}),
            "dev": gguf(folder / "dev.gguf", {"img_in.weight": (6144, 128), "double_blocks.0.img_attn.proj.weight": (6144, 6144)}),
            "flux1": gguf(folder / "flux1.gguf", {"img_in.weight": (3072, 64), "double_blocks.0.x": (3072,)}),
            "qwen4": gguf(folder / "qwen3-4b.gguf", {"token_embd.weight": (151936, 2560)}),
            "qwen8": gguf(folder / "qwen3-8b.gguf", {"model.embed_tokens.weight": (151936, 4096)}),
            "vae": safetensors(folder / "flux2-vae.safetensors", {"decoder.conv_in.weight": (512, 32, 3, 3), "encoder.conv_in.weight": (128, 3, 3, 3)}),
            "sdxl": safetensors(folder / "sdxl.safetensors", {"model.diffusion_model.label_emb.0.0.weight": (1280, 2816), "conditioner.embedders.1.model.ln_final.weight": (1280,),
                                                              "first_stage_model.decoder.conv_in.weight": (512, 4, 3, 3)}),
            "lora": safetensors(folder / "style.safetensors", {"diffusion_model.double_blocks.0.img_attn.proj.lora_B.weight": (4096, 32)})}


def draft(diffusion, family, **components):
    return {"engine": "sdcpp", "flags": {"model": diffusion, "ctx-size": 0}, "sdcpp": {"family": family, "components": components}}


def test_parts_are_told_apart_by_their_headers(library):
    assert [identify(library[key])["variant"] for key in ("klein9", "dev", "qwen4", "qwen8", "vae")] == ["klein-9b", "dev", "qwen3-4b", "qwen3-8b", "flux2-vae"]
    assert identify(library["sdxl"])["family"] == "sdxl" and identify(library["sdxl"])["variant"] == "checkpoint"
    assert identify(library["lora"])["kind"] == "lora" and identify(library["lora"])["variant"] == "klein-9b"
    assert identify(library["flux1"])["family"] == "flux1"
    assert identify(str(library["folder"] / "absent.gguf"))["kind"] == "missing"


@pytest.mark.parametrize('family,encoder_shape,vae_channels',[
    ('qwen-image',(152064,3584),16),('qwen-image-2.1',(151936,4096),64)])
def test_qwen_parts_and_component_suggestions_are_distinct_from_flux(library,family,encoder_shape,vae_channels):
    folder=library['folder']
    tensors={'img_in.weight':(4096,64),'transformer_blocks.0.img_mod.1.weight':(12288,4096)}
    if family=='qwen-image-2.1': tensors['txt_in.text_norm.weight']=(4096,)
    weights=safetensors(folder/'qwen-image.safetensors',tensors)
    encoder=safetensors(folder/'qwen-vl.safetensors',{'model.language_model.embed_tokens.weight':encoder_shape})
    vae=safetensors(folder/'qwen-vae.safetensors',{'decoder.conv_in.weight':(512,vae_channels,3,3,3)})
    assert identify(weights)['family']==family
    found=check(draft(weights,family,text_encoder=encoder,vae=vae),folder)
    assert found['ready']
    wrong=check(draft(weights,family,text_encoder=library['qwen8'],vae=library['vae']),folder)
    slots={one['slot']:one for one in wrong['slots']}
    assert slots['text_encoder']['status']==slots['vae']['status']=='wrong'
    assert slots['text_encoder']['candidates']==[encoder]
    assert 'Qwen' in slots['text_encoder']['message'] and slots['vae']['get']['repository'].startswith('Comfy-Org/Qwen-Image')


def test_qwen3_vl_gguf_metadata_disambiguates_identical_text_embedding_shapes(library):
    path=gguf(library['folder']/'qwen-vl.gguf',{'token_embd.weight':(151936,4096)}, {'general.architecture':'qwen3vl'})
    assert identify(path)['variant']=='qwen3-vl-8b'
    assert identify(library['qwen8'])['variant']=='qwen3-8b'
    vl=gguf(library['folder']/'qwen25-vl.gguf',{'token_embd.weight':(152064,3584)}, {'general.architecture':'qwen2vl'})
    text=gguf(library['folder']/'qwen25-text.gguf',{'token_embd.weight':(152064,3584)}, {'general.architecture':'qwen2'})
    assert identify(vl)['variant']=='qwen2.5-vl-7b'
    assert identify(text)['variant']=='qwen2.5-7b'


def test_the_check_names_the_wrong_part_and_offers_the_right_one_already_there(library):
    found = check(draft(library["klein9"], "flux2-klein", text_encoder=library["qwen4"], vae=library["vae"]), library["folder"])
    slots = {one["slot"]: one for one in found["slots"]}
    assert found["detected_family"] == "flux2-klein" and found["variant_label"] == "Flux 2 klein 9B" and not found["ready"]
    assert slots["text_encoder"]["status"] == "wrong" and "Qwen3 8B" in slots["text_encoder"]["message"]
    assert slots["text_encoder"]["candidates"] == [library["qwen8"]] and slots["text_encoder"]["get"]["repository"] == "unsloth/Qwen3-8B-GGUF"
    assert slots["vae"]["status"] == "ok" and slots["clip_l"]["need"] == "unused" and slots["upscaler"]["need"] == "optional"
    fixed = check(draft(library["klein9"], "flux2-klein", text_encoder=library["qwen8"], vae=library["vae"]) | {}, library["folder"])
    assert fixed["ready"]
    missing = check(draft(library["dev"], "flux2-dev"), library["folder"])
    assert {one["slot"]: one["status"] for one in missing["slots"]}["text_encoder"] == "missing" and not missing["ready"]


def test_a_family_that_disagrees_with_the_weights_or_an_unsupported_model_is_not_ready(library):
    wrong = check(draft(library["klein9"], "flux2-dev", text_encoder=library["qwen8"], vae=library["vae"]), library["folder"])
    assert wrong["detected_family"] == "flux2-klein" and not wrong["family_matches"] and not wrong["ready"]
    flux1 = check(draft(library["flux1"], "flux2-klein"), library["folder"])
    assert flux1["slots"][0]["status"] == "wrong" and "Flux 2 klein, Flux 2 Dev and SDXL" in flux1["slots"][0]["message"]


def test_an_sdxl_checkpoint_needs_nothing_else_and_a_lora_must_match_its_base(library):
    whole = check(draft(library["sdxl"], "sdxl"), library["folder"])
    slots = {one["slot"]: one for one in whole["slots"]}
    assert whole["ready"] and slots["text_encoder"]["need"] == "unused" and slots["vae"]["need"] == "optional"
    assert slots["vae"]["get"]["repository"] == "madebyollin/sdxl-vae-fp16-fix"
    settings = draft(library["dev"], "flux2-dev", vae=library["vae"])
    settings["sdcpp"]["loras"] = {"style": {"file": library["lora"]}}
    assert check(settings, library["folder"])["loras"][0]["status"] == "wrong"


def test_files_outside_the_model_folders_are_not_read(library, tmp_path):
    elsewhere = gguf(tmp_path / "secret.gguf", {"img_in.weight": (4096, 128), "double_blocks.0.x": (4096,)})
    found = check(draft(elsewhere, "flux2-klein"), library["folder"])
    assert found["slots"][0]["found"]["label"] == "Outside this node's model folders" and not found["ready"]


def test_library_symlinks_cannot_bypass_the_model_roots(library, tmp_path, monkeypatch):
    from mirrorwall import components
    outside = Path(safetensors(tmp_path / "outside.safetensors", {"token_embd.weight": (151936, 4096)}))
    link = library["folder"] / "outside-link.safetensors"
    link.symlink_to(outside)
    original = components.shapes_of
    def guarded(path):
        assert path.resolve() != outside, "The library scan opened a file outside the model roots."
        return original(path)
    monkeypatch.setattr(components, "shapes_of", guarded)
    components._identified.cache_clear()
    found = check(draft(library["klein9"], "flux2-klein"), library["folder"])
    encoder = next(slot for slot in found["slots"] if slot["slot"] == "text_encoder")
    assert str(link) not in encoder["candidates"]


def test_malformed_tensor_dimensions_are_reported_as_unreadable(tmp_path):
    malformed = safetensors(tmp_path / "malformed.safetensors", {"text_model.embeddings.token_embedding.weight": (49408,)})
    assert identify(malformed, [tmp_path])["kind"] == "unreadable"


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, "1", True])
def test_invalid_reference_sizes_fail_before_the_job_is_queued(value):
    front = SimpleNamespace(store=SimpleNamespace(asset=lambda *args: {"width": 640, "height": 480}))
    selected = {"id": "image", "image_workflow": {"reference_megapixels": 1}}
    with pytest.raises(ValueError, match="finite number"):
        Front.fits(front, selected, {"references": ["ref"], "request": {"reference_megapixels": value}}, SimpleNamespace(identity="owner"))


def test_an_sdxl_checkpoint_loads_whole_and_takes_one_start_image(library):
    settings = {"engine": "sdcpp", "flags": {"model": library["sdxl"], "ctx-size": 0}, "gpus": 1, "sdcpp": {"family": "sdxl", "components": {}}}
    job = {"settings": settings, "installation": {"executable": "/native/sd-server"}}
    command = native_command(job, 1234)
    assert command[command.index("--model") + 1] == library["sdxl"] and "--diffusion-model" not in command
    assert model.limits(settings)["max_references"] == 1
    with pytest.raises(ValueError, match="one image"):
        model.check_model("sdxl", {**settings, "sdcpp": {**settings["sdcpp"], "limits": {"max_references": 2}}}, ("CUDA0",))
    flux = {"engine": "sdcpp", "flags": {"model": library["klein9"], "ctx-size": 0}, "sdcpp": {"family": "flux2-klein", "components": {"vae": library["vae"]}}}
    assert "--diffusion-model" in native_command({**job, "settings": flux}, 1234)


def test_front_refuses_references_the_model_would_refuse_later():
    sizes = {"big": (1536, 1024), "small": (512, 512)}
    front = SimpleNamespace(store=SimpleNamespace(asset=lambda owner, identifier: {"width": sizes[identifier][0], "height": sizes[identifier][1]}))
    user = SimpleNamespace(identity="owner")
    plain = {"id": "klein", "image_workflow": {"limits": {"max_references": 2}, "reference_megapixels": 1, "upscaling": False}}
    Front.fits(front, plain, {"references": ["big"]}, user)
    with pytest.raises(ValueError, match="no upscaler"):
        Front.fits(front, plain, {"references": ["big", "small"]}, user)
    Front.fits(front, plain | {"image_workflow": {**plain["image_workflow"], "upscaling": True}}, {"references": ["small"]}, user)
    with pytest.raises(ValueError, match="at most 2"):
        Front.fits(front, plain, {"references": ["big", "big", "big"]}, user)
    with pytest.raises(ValueError, match="does not use reference"):
        Front.fits(front, {"id": "none", "image_workflow": {"limits": {"max_references": 0}}}, {"references": ["big"]}, user)
    with pytest.raises(ValueError, match="mask needs a reference"):
        Front.fits(front, plain, {"mask": "m"}, user)


def test_repeated_large_library_scan_reuses_unchanged_headers(tmp_path, monkeypatch):
    from mirrorwall import components
    for index in range(1100):
        safetensors(tmp_path / f"part-{index:04}.safetensors", {"decoder.conv_in.weight": (512, 32, 3, 3)})
    reads = []
    original = components.shapes_of
    def counted(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(components, "shapes_of", counted)
    components._identified.cache_clear()
    try:
        first = components.scan(str(tmp_path))
        assert len(first) == 1100 and len(reads) == 1100
        assert components.scan(str(tmp_path)) == first
        assert len(reads) == 1100
        changed = tmp_path / "part-0000.safetensors"
        safetensors(changed, {"decoder.conv_in.weight": (512, 4, 3, 3)})
        assert components.identify(str(changed))["variant"] == "sdxl-vae"
        assert len(reads) == 1101
    finally:
        components._identified.cache_clear()
