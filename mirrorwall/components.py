"""What an image model file is, from its header alone, and what a profile still needs to run.

Nothing here loads weights or unpickles anything. GGUF and safetensors headers name every tensor
and its shape, which is enough to tell a Flux 2 transformer from a text encoder or a VAE, and
which size of each. A .pth file is taken to be an upscaler by its suffix and is never opened.
"""
import json
import struct
from functools import lru_cache
from itertools import islice
from pathlib import Path

from engines.llama_cpp.gguf import read_metadata

PREFIXES = ("model.diffusion_model.", "diffusion_model.", "transformer.", "unet.")
FLUX2 = {3072: "klein-4b", 4096: "klein-9b", 6144: "dev"}  # transformer width -> variant
ENCODER = {"klein-4b": "qwen3-4b", "klein-9b": "qwen3-8b", "dev": "mistral-small-24b"}
LABELS = {"klein-4b": "Flux 2 klein 4B", "klein-9b": "Flux 2 klein 9B", "dev": "Flux 2 Dev", "checkpoint": "SDXL checkpoint (UNet, CLIP and VAE in one file)",
          "unet": "SDXL UNet only", "qwen3-4b": "Qwen3 4B text encoder", "qwen3-8b": "Qwen3 8B text encoder",
          "mistral-small-24b": "Mistral Small 3.x 24B text encoder", "flux2-vae": "Flux 2 VAE (32 latent channels)",
          "sdxl-vae": "SD/SDXL VAE (4 latent channels)", "clip_l": "CLIP L text encoder", "clip_g": "CLIP G text encoder"}
LABELS.update({'qwen-image':'Qwen Image (original / 2512)', 'qwen-image-2.1':'Qwen Image 2.1',
               'qwen2.5-vl-7b':'Qwen2.5-VL 7B text encoder','qwen3-vl-8b':'Qwen3-VL 8B text encoder',
               'qwen-image-vae':'Qwen Image VAE (16 latent channels)', 'qwen-image-2.1-vae':'Qwen Image 2.1 VAE (64 latent channels)'})
# Where to get each missing part: a repository and file the Hugging Face page can bring down.
GET = {"klein-4b": ("leejet/FLUX.2-klein-4B-GGUF", "flux-2-klein-4b-Q8_0.gguf"),
       "klein-9b": ("leejet/FLUX.2-klein-9B-GGUF", "flux-2-klein-9b-Q4_0.gguf"),
       "dev": ("city96/FLUX.2-dev-gguf", "flux2-dev-Q4_K_S.gguf"),
       "qwen3-4b": ("unsloth/Qwen3-4B-GGUF", "Qwen3-4B-Q8_0.gguf"),
       "qwen3-8b": ("unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf"),
       "mistral-small-24b": ("unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF", "Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf"),
       "flux2-vae": ("Comfy-Org/Flux-2-Dev", "split_files/vae/flux2-vae.safetensors"),
       "sdxl-vae": ("madebyollin/sdxl-vae-fp16-fix", "sdxl_vae.safetensors"),
       "checkpoint": ("stabilityai/stable-diffusion-xl-base-1.0", "sd_xl_base_1.0.safetensors"),
       "upscaler": ("Kim2091/UltraSharp", "4x-UltraSharp.pth")}
GET.update({'qwen-image':('QuantStack/Qwen-Image-GGUF','Qwen_Image-Q4_K_M.gguf'),
            'qwen-image-2.1':('leejet/Qwen-Image-2.1-GGUF','qwen_image_2.1-Q4_K.gguf'),
            'qwen2.5-vl-7b':('mradermacher/Qwen2.5-VL-7B-Instruct-GGUF','Qwen2.5-VL-7B-Instruct.Q8_0.gguf'),
            'qwen3-vl-8b':('Qwen/Qwen3-VL-8B-Instruct-GGUF','Qwen3VL-8B-Instruct-Q4_K_M.gguf'),
            'qwen-image-vae':('Comfy-Org/Qwen-Image_ComfyUI','split_files/vae/qwen_image_vae.safetensors'),
            'qwen-image-2.1-vae':('Comfy-Org/Qwen-Image-2.1','vae/qwen_image_2.1_vae_bf16.safetensors')})
SLOTS = (("diffusion", "Diffusion weights"), ("text_encoder", "Text encoder"), ("vae", "VAE"),
         ("clip_l", "CLIP L"), ("clip_g", "CLIP G"), ("upscaler", "Image upscaler"))
FORMATS = (".gguf", ".safetensors", ".pth")


def shapes_of(path):
    """Tensor names and shapes, outermost dimension first as PyTorch writes them, from the header alone."""
    if path.suffix.lower() == ".gguf":
        return {name: tuple(reversed(dims)) for name, dims in read_metadata(str(path), shapes=True)["tensors"].items()}
    with path.open("rb") as file:
        size = struct.unpack("<Q", file.read(8))[0]
        if size > 100 * 2**20:
            raise ValueError("not a safetensors header")
        header = json.loads(file.read(size))
    return {name: tuple(entry["shape"]) for name, entry in header.items() if name != "__metadata__"}


def plain(name):
    """A tensor name without the wrapper prefixes converters add."""
    for prefix in PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def classify(shapes):
    """(kind, family, variant) from tensor names and shapes; family is None for parts other than diffusion weights."""
    names = set(shapes)
    if any("lora_" in name or ".lora." in name for name in names):
        width = next((shape[0] for name, shape in shapes.items() if "img_attn.proj" in name and ("lora_B" in name or "lora_up" in name)), None)
        return "lora", "flux2" if any("double_blocks" in name for name in names) else None, FLUX2.get(width)
    flat = {plain(name): shape for name, shape in shapes.items()}
    if 'txt_in.text_norm.weight' in flat:
        return 'diffusion','qwen-image-2.1','qwen-image-2.1'
    if 'transformer_blocks.0.img_mod.1.weight' in flat and flat.get('img_in.weight',(0,0))[1] == 64:
        if 'time_text_embed.addition_t_embedding.weight' in flat:
            return 'diffusion','qwen-image-layered',None
        return 'diffusion','qwen-image','qwen-image'
    if "img_in.weight" in flat and any(name.startswith("double_blocks") for name in flat):
        width, channels = flat["img_in.weight"][:2]
        if channels != 128:
            return "diffusion", "flux1", None  # 64 input channels: Flux 1, which Mirror Wall does not run
        variant = FLUX2.get(width)
        return "diffusion", "flux2-dev" if variant == "dev" else "flux2-klein" if variant else "flux2", variant
    if any(name.startswith("cond_stage_model.") for name in names):
        return "diffusion", "sd1", None
    if "label_emb.0.0.weight" in flat or "add_embedding.linear_1.weight" in flat:
        whole = any(name.startswith("conditioner.embedders.1") for name in names) and any(name.startswith("first_stage_model.") for name in names)
        return "diffusion", "sdxl", "checkpoint" if whole else "unet"
    conv = next((shape for name, shape in shapes.items() if name.endswith("decoder.conv_in.weight")), None)
    if conv and len(conv) in (4,5):
        return "vae", None, {32: "flux2-vae", 4: "sdxl-vae",16:'qwen-image-vae',64:'qwen-image-2.1-vae'}.get(conv[1], f"vae-{conv[1]}")
    token = shapes.get("text_model.embeddings.token_embedding.weight") or shapes.get("token_embedding.weight")
    if token and token[0] == 49408:
        return {768: ("clip_l", None, "clip_l"), 1280: ("clip_g", None, "clip_g")}.get(token[1], ("unknown", None, None))
    embed = shapes.get('model.language_model.embed_tokens.weight') or shapes.get('language_model.model.embed_tokens.weight') or shapes.get("model.embed_tokens.weight") or shapes.get("token_embd.weight")
    if embed and len(embed) == 2:
        vocabulary, width = embed
        if vocabulary==152064 and width==3584:
            vision=any(name.startswith(('model.visual.','visual.','model.language_model.','language_model.')) for name in names)
            return 'text_encoder',None,'qwen2.5-vl-7b' if vision else 'qwen2.5-7b'
        if width==4096 and any(name.startswith(('model.visual.','visual.','model.language_model.','language_model.')) for name in names):
            return 'text_encoder',None,'qwen3-vl-8b'
        if vocabulary in (151936, 151669):
            return "text_encoder", None, {2560: "qwen3-4b", 4096: "qwen3-8b"}.get(width, f"qwen-{width}")
        if vocabulary == 131072 and width == 5120:
            return "text_encoder", None, "mistral-small-24b"
        return "text_encoder", None, f"llm-{width}"
    if "conv_first.weight" in names or any(name.startswith(("model.0.", "body.")) for name in names):
        return "upscaler", None, None
    return "unknown", None, None


# Keep one bounded library scan plus its selected components without LRU churn.
@lru_cache(maxsize=2048)
def _identified(path, modified, size):
    """One file's identity, cached by its revision."""
    file = Path(path)
    if file.suffix.lower() == ".pth":
        return {"kind": "upscaler", "family": None, "variant": None, "label": "Image upscaler (.pth, ESRGAN family)"}
    try:
        kind, family, variant = classify(shapes_of(file))
        if file.suffix.lower()=='.gguf' and kind=='text_encoder' and variant in ('qwen2.5-7b','qwen3-8b'):
            architecture=read_metadata(str(file)).get('general.architecture')
            if variant=='qwen2.5-7b' and architecture=='qwen2vl':
                variant='qwen2.5-vl-7b'
            elif variant=='qwen3-8b' and architecture=='qwen3vl':
                variant='qwen3-vl-8b'
    except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error):
        return {"kind": "unreadable", "family": None, "variant": None, "label": "Not a readable GGUF or safetensors file"}
    label = LABELS.get(variant) or {"lora": "LoRA adapter", "upscaler": "Image upscaler", "flux1": "Flux 1 (not supported here)",
                                    "sd1": "Stable Diffusion 1.x/2.x (not supported here)"}.get(family or kind, kind.replace("_", " "))
    if kind == "lora":
        label = "LoRA for " + LABELS[variant] if variant else "LoRA adapter"
    return {"kind": kind, "family": family, "variant": variant, "label": label}


def identify(path, roots=None):
    """What one file is, or that there is no such file. With `roots`, a file outside those folders is not looked at."""
    file = Path(path)
    if roots is not None and not any(file.resolve().is_relative_to(Path(root).resolve()) for root in roots if root):
        return {"kind": "unreadable", "family": None, "variant": None, "label": "Outside this node's model folders"}
    try:
        stat = file.stat()
    except OSError:
        return {"kind": "missing", "family": None, "variant": None, "label": "No such file on this compute node"}
    if not file.is_file() or file.suffix.lower() not in FORMATS:
        return {"kind": "unreadable", "family": None, "variant": None, "label": "Not a .gguf, .safetensors or .pth file"}
    return {**_identified(str(file), stat.st_mtime_ns, stat.st_size), "bytes": stat.st_size}


def scan(folder, roots=None):
    """Identify at most 2,000 library files, respecting the same roots as the selected parts."""
    root = Path(folder)
    if not folder or not root.is_dir():
        return []
    roots = [folder] if roots is None else roots
    files = islice((file for file in root.rglob("*") if file.suffix.lower() in FORMATS and file.is_file()), 2000)
    return sorted([{"path": str(file), **identify(file, roots)} for file in files], key=lambda row: row["path"])


def needs(family, variant):
    """What each slot must hold: (required, expected kind, expected variant, why) by slot."""
    upscaler = (False, "upscaler", None, "Enlarges reference images smaller than the reference size, and outputs when asked to upscale.")
    if family in ('qwen-image','qwen-image-2.1'):
        encoder='qwen3-vl-8b' if family=='qwen-image-2.1' else 'qwen2.5-vl-7b'
        vae='qwen-image-2.1-vae' if family=='qwen-image-2.1' else 'qwen-image-vae'
        return {'text_encoder':(True,'text_encoder',encoder,f'{LABELS[family]} needs {LABELS[encoder]}, not a Qwen3 text-only encoder.'),
                'vae':(True,'vae',vae,'Qwen Image VAE generations are not interchangeable; Flux 2 and SDXL VAEs do not fit.'),
                'clip_l':None,'clip_g':None,'upscaler':upscaler}
    if family in ("flux2-klein", "flux2-dev") and variant in ENCODER:
        return {"text_encoder": (True, "text_encoder", ENCODER[variant], f"{LABELS[variant]} reads prompts with the {LABELS[ENCODER[variant]]}."),
                "vae": (True, "vae", "flux2-vae", "Every Flux 2 model decodes images with the Flux 2 VAE."),
                "clip_l": None, "clip_g": None, "upscaler": upscaler}
    if family == "sdxl" and variant == "checkpoint":
        return {"text_encoder": None, "clip_l": None, "clip_g": None, "upscaler": upscaler,
                "vae": (False, "vae", "sdxl-vae", "The checkpoint has its own VAE; the fp16-fix VAE avoids black or noisy images at half precision.")}
    if family == "sdxl":
        return {"text_encoder": None, "upscaler": upscaler,
                "vae": (True, "vae", "sdxl-vae", "A UNet-only file has no VAE of its own."),
                "clip_l": (True, "clip_l", "clip_l", "A UNet-only file has no text encoders of its own."),
                "clip_g": (True, "clip_g", "clip_g", "A UNet-only file has no text encoders of its own.")}
    return {"text_encoder": None, "vae": None, "clip_l": None, "clip_g": None, "upscaler": upscaler}


def check(settings, folder, roots=None):
    """Each slot of a draft image profile: what it holds, whether it fits the diffusion weights, and what to get or use instead.

    `folder` is searched for files that would fit a slot; `roots` are the only folders whose files are read.
    """
    roots = [folder] if roots is None else roots
    options = settings.get("sdcpp", {}) if isinstance(settings, dict) else {}
    chosen = {"diffusion": (settings.get("flags") or {}).get("model", "") if isinstance(settings, dict) else "",
              **{key: value for key, value in (options.get("components") or {}).items() if isinstance(value, str)},
              "upscaler": options.get("upscaler") or ""}
    library = scan(folder, roots)
    diffusion = identify(chosen["diffusion"], roots) if chosen["diffusion"] else None
    family, variant = (diffusion or {}).get("family"), (diffusion or {}).get("variant")
    wanted = needs(family, variant)
    slots, ready = [], True
    for slot, label in SLOTS:
        path = chosen.get(slot, "")
        found = identify(path, roots) if path else None
        rule = (True, "diffusion", None, "The model itself.") if slot == "diffusion" else wanted.get(slot)
        entry = {"slot": slot, "label": label, "path": path, "found": found, "need": "unused" if rule is None else "required" if rule[0] else "optional",
                 "why": rule[3] if rule else ""}
        expected_kind, expected = (rule[1], rule[2]) if rule else (None, None)
        get = GET.get(expected or ("upscaler" if slot == "upscaler" else ""))
        if slot == "diffusion" and family in ("flux1", "sd1",'qwen-image-layered'):
            entry["status"], entry["message"] = "wrong", found["label"] + ": Mirror Wall runs Flux 2 klein, Flux 2 Dev and SDXL, plus Qwen Image text-to-image profiles."
        elif slot == "diffusion" and found and found["kind"] == "diffusion" and not variant:
            entry["status"], entry["message"] = "wrong", "Diffusion weights of an unrecognized size; Mirror Wall knows Flux 2 klein 4B and 9B, Flux 2 Dev, SDXL, Qwen Image and Qwen Image 2.1."
        elif found and found["kind"] in ("missing", "unreadable"):
            entry["status"], entry["message"] = "wrong", found["label"] + (": copy the file to this node from Files, or correct the path." if found["kind"] == "missing" else ".")
        elif found and rule is None:
            entry["status"], entry["message"] = "unused", f"{found['label']}: this model does not use a {label}; leave it empty."
        elif found and (found["kind"] != expected_kind or expected and found["variant"] != expected):
            entry["status"] = "wrong"
            entry["message"] = f"This is a {found['label']}; " + (f"it needs the {LABELS.get(expected, expected)}." if expected else f"this slot needs {label.lower()}.")
        elif found:
            entry["status"], entry["message"] = "ok", found["label"]
        elif rule and rule[0]:
            entry["status"], entry["message"] = "missing", f"Needed: the {LABELS.get(expected, label)}." if slot != "diffusion" else "Choose the diffusion weights first."
        else:
            entry["status"], entry["message"] = ("optional", "Optional.") if rule else ("unused", "Not used by this model.")
        if entry["status"] in ("missing", "wrong", "optional") and rule:
            if get and slot != "diffusion":
                entry["get"] = {"repository": get[0], "file": get[1]}
            entry["candidates"] = [one["path"] for one in library if one["kind"] == expected_kind and (expected is None or one["variant"] == expected)
                                   and one["path"] != path][:6]
        ready &= entry["status"] in ("ok", "optional", "unused") and not (slot != "diffusion" and entry["status"] == "unused" and found)
        slots.append(entry)
    loras = []
    for name, entry in (options.get("loras") or {}).items():
        found = identify(entry.get("file", ""), roots) if isinstance(entry, dict) else None
        fits = bool(found and found["kind"] == "lora" and (found["variant"] in (None, variant) or not variant))
        loras.append({"name": name, "found": found, "status": "ok" if fits else "wrong",
                      "message": found["label"] if fits else (found or {}).get("label", "Not a LoRA") + (f"; this model is {LABELS.get(variant, variant)}." if found and found["kind"] == "lora" else ".")})
        ready &= fits
    detected = family if family in ("flux2-klein", "flux2-dev", "sdxl",'qwen-image','qwen-image-2.1') else None
    if detected and options.get("family") and options["family"] != detected:
        ready = False
    return {"detected_family": detected, "variant": variant, "variant_label": LABELS.get(variant, ""), "family_matches": not detected or options.get("family") == detected,
            "slots": slots, "loras": loras, "ready": bool(ready and diffusion and detected), "folder": str(folder or ""),
            "get_diffusion": [{"label": LABELS[key], "repository": GET[key][0], "file": GET[key][1]} for key in ("klein-4b", "klein-9b", "dev", "checkpoint",'qwen-image','qwen-image-2.1')]}
