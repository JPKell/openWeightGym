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
from mirrorwall.assets import tokenizer_asset

PREFIXES = ("model.diffusion_model.", "diffusion_model.", "transformer.", "unet.")
FLUX2 = {3072: "klein-4b", 4096: "klein-9b", 6144: "dev"}  # transformer width -> variant
ENCODER = {"klein-4b": "qwen3-4b", "klein-9b": "qwen3-8b", "dev": "mistral-small-24b"}
LABELS = {"klein-4b": "Flux 2 klein 4B", "klein-9b": "Flux 2 klein 9B", "dev": "Flux 2 Dev", "checkpoint": "SDXL checkpoint (UNet, CLIP and VAE in one file)",
          "unet": "SDXL UNet only", "qwen3-4b": "Qwen3 4B text encoder", "qwen3-8b": "Qwen3 8B text encoder",
          "mistral-small-24b": "Mistral Small 3.x 24B text encoder", "flux2-vae": "Flux 2 VAE (32 latent channels)",
          "sdxl-vae": "SD/SDXL VAE (4 latent channels)", "clip_l": "CLIP L text encoder", "clip_g": "CLIP G text encoder",
          "sdxl-controlnet": "SDXL ControlNet", "sd1-controlnet": "SD 1.x ControlNet"}
LABELS.update({'qwen-image':'Qwen Image (original / 2512)', 'qwen-image-2.1':'Qwen Image 2.1',
               'qwen2.5-vl-7b':'Qwen2.5-VL 7B text encoder','qwen3-vl-8b':'Qwen3-VL 8B text encoder',
               'qwen-image-vae':'Qwen Image VAE (16 latent channels)', 'qwen-image-2.1-vae':'Qwen Image 2.1 VAE (64 latent channels)'})
LABELS.update({'moss-image': 'MOSS-Image (Base / SFT)', 'moss-image-base': 'MOSS-Image Base',
               'moss-image-sft': 'MOSS-Image SFT', 'qwen3.5-9b': 'Qwen3.5 9B text encoder',
               'qwen3.5-tokenizer': 'Qwen3.5 tokenizer (JSON)'})
LABELS.update({'krea2': 'Krea 2 (Raw / Turbo)', 'krea2-raw': 'Krea 2 Raw', 'krea2-turbo': 'Krea 2 Turbo',
               'qwen3-vl-4b': 'Qwen3-VL 4B text encoder'})
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
       "upscaler": ("Kim2091/UltraSharp", "4x-UltraSharp.pth"),
       "sdxl-controlnet": ("diffusers/controlnet-canny-sdxl-1.0", "diffusion_pytorch_model.fp16.safetensors")}
GET.update({'qwen-image':('QuantStack/Qwen-Image-GGUF','Qwen_Image-Q4_K_M.gguf'),
            'qwen-image-2.1':('leejet/Qwen-Image-2.1-GGUF','qwen_image_2.1-Q4_K.gguf'),
            'qwen2.5-vl-7b':('mradermacher/Qwen2.5-VL-7B-Instruct-GGUF','Qwen2.5-VL-7B-Instruct.Q8_0.gguf'),
            'qwen3-vl-8b':('Qwen/Qwen3-VL-8B-Instruct-GGUF','Qwen3VL-8B-Instruct-Q4_K_M.gguf'),
            'qwen-image-vae':('Comfy-Org/Qwen-Image_ComfyUI','split_files/vae/qwen_image_vae.safetensors'),
            'qwen-image-2.1-vae':('Comfy-Org/Qwen-Image-2.1','vae/qwen_image_2.1_vae_bf16.safetensors')})
GET.update({'moss-image-base': ('leejet/MOSS-Image-Base-GGUF', 'MOSS-Image-Base-Q4_K.gguf'),
            'moss-image-sft': ('leejet/MOSS-Image-SFT-GGUF', 'MOSS-Image-SFT-Q4_K.gguf'),
            'qwen3.5-9b': ('unsloth/Qwen3.5-9B-GGUF', 'Qwen3.5-9B-Q4_K_M.gguf'),
            'qwen3.5-tokenizer': ('OpenMOSS-Team/MOSS-Image-Base', 'tokenizer/tokenizer.json')})
GET.update({'krea2-raw': ('realrebelai/KREA-2_GGUFs', 'BASE/Krea-2-Base-Q4_K_M.gguf'),
            'krea2-turbo': ('realrebelai/KREA-2_GGUFs', 'TURBO/Krea-2-Turbo-Q4_K_M.gguf'),
            'qwen3-vl-4b': ('Qwen/Qwen3-VL-4B-Instruct-GGUF', 'Qwen3VL-4B-Instruct-Q4_K_M.gguf'),
            'wan2.1-vae': ('Comfy-Org/Wan_2.1_ComfyUI_repackaged', 'split_files/vae/wan_2.1_vae.safetensors')})
SLOTS = (("diffusion", "Diffusion weights"), ("text_encoder", "Text encoder"), ("vae", "VAE"),
         ("clip_l", "CLIP L"), ("clip_g", "CLIP G"), ("upscaler", "Image upscaler"), ("tokenizer", "Tokenizer JSON"),
         ("control_net", "ControlNet"))
FORMATS = (".gguf", ".safetensors", ".pth", ".json")
IDENTITY_VERSION = 6  # ControlNets (5: Krea 2 transformers and Qwen3-VL 4B encoders; 4: video parts)
VIDEO_SLOTS=(("diffusion","Diffusion weights"),("text_encoder","Text encoder"),("vae","VAE"),
             ("high_noise_diffusion_model","High-noise diffusion weights"),("audio_vae","Audio VAE"),
             ("embeddings_connectors","Embeddings connector"),("byt5","ByT5 GlyphXL"),
             ("clip_vision","CLIP Vision"),("latent_upscaler","Latent upscaler"),("tae","Preview decoder"))
LABELS.update({'t2v-1.3b':'WAN 2.1 T2V 1.3B','t2v-14b':'WAN T2V 14B','i2v-14b':'WAN I2V 14B',
               'flf2v-14b':'WAN first/last-frame 14B','ti2v-5b':'WAN 2.2 TI2V 5B',
               'wan2.1-vae':'WAN 2.1 VAE','wan2.2-vae':'WAN 2.2 TI2V VAE','umt5-xxl':'umT5 XXL text encoder',
               'ltx-2.3':'LTX-2.3','ltx-2.5':'LTX-2.5','ltx-conv-vae':'LTX conv video VAE',
               'hunyuan-video-1.5':'HunyuanVideo 1.5','minimax-h3':'MiniMax-H3'})


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
    video=classify_video(flat)
    if video:
        return video
    if (flat.get('video_embedder.weight') == (3072, 128)
            and flat.get('text_embedder.proj.weight') == (3072, 4096)
            and any(name.startswith('layers.') for name in flat)):
        return 'diffusion', 'moss-image', 'moss-image'
    if 'txtfusion.projector.weight' in flat or 'text_fusion.projector.weight' in flat:
        return 'diffusion', 'krea2', 'krea2'  # Raw and Turbo share one architecture
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
    if any(name.startswith(("controlnet_cond_embedding.", "control_model.input_hint_block.", "input_hint_block.")) for name in names):
        # A ControlNet copies the UNet's encoder, so check it before the UNet: SDXL ones carry its extra embedding.
        sdxl = any(name.startswith(("add_embedding.", "control_model.label_emb.", "label_emb.")) for name in names)
        return "controlnet", None, "sdxl-controlnet" if sdxl else "sd1-controlnet"
    if "label_emb.0.0.weight" in flat or "add_embedding.linear_1.weight" in flat:
        # sd.cpp's GGUF converter names SDXL's two CLIPs cond_stage_model and
        # cond_stage_model.1; safetensors checkpoints use conditioner.embedders.
        clips = any(name.startswith("conditioner.embedders.1") for name in names) or (
            shapes.get('cond_stage_model.transformer.text_model.embeddings.token_embedding.weight') == (49408, 768)
            and shapes.get('cond_stage_model.1.transformer.text_model.embeddings.token_embedding.weight') == (49408, 1280))
        whole = clips and any(name.startswith("first_stage_model.") for name in names)
        return "diffusion", "sdxl", "checkpoint" if whole else "unet"
    if any(name.startswith("cond_stage_model.") for name in names):
        return "diffusion", "sd1", None
    conv = next((shape for name, shape in shapes.items() if name.endswith("decoder.conv_in.weight")), None)
    if conv and len(conv) in (4,5):
        return "vae", None, {32: "flux2-vae", 4: "sdxl-vae",16:'qwen-image-vae',64:'qwen-image-2.1-vae'}.get(conv[1], f"vae-{conv[1]}")
    token = shapes.get("text_model.embeddings.token_embedding.weight") or shapes.get("token_embedding.weight")
    if token and token[0] == 49408:
        return {768: ("clip_l", None, "clip_l"), 1280: ("clip_g", None, "clip_g")}.get(token[1], ("unknown", None, None))
    embed = shapes.get('model.language_model.embed_tokens.weight') or shapes.get('language_model.model.embed_tokens.weight') or shapes.get("model.embed_tokens.weight") or shapes.get("token_embd.weight")
    if embed and len(embed) == 2:
        vocabulary, width = embed
        if vocabulary == 248320 and width == 4096:
            return 'text_encoder', None, 'qwen3.5-9b'
        if vocabulary==152064 and width==3584:
            vision=any(name.startswith(('model.visual.','visual.','model.language_model.','language_model.')) for name in names)
            return 'text_encoder',None,'qwen2.5-vl-7b' if vision else 'qwen2.5-7b'
        if width in (2560,4096) and any(name.startswith(('model.visual.','visual.','model.language_model.','language_model.')) for name in names):
            return 'text_encoder',None,'qwen3-vl-4b' if width==2560 else 'qwen3-vl-8b'
        if vocabulary in (151936, 151669):
            return "text_encoder", None, {2560: "qwen3-4b", 4096: "qwen3-8b"}.get(width, f"qwen-{width}")
        if vocabulary == 131072 and width == 5120:
            return "text_encoder", None, "mistral-small-24b"
        return "text_encoder", None, f"llm-{width}"
    if "conv_first.weight" in names or any(name.startswith(("model.0.", "body.")) for name in names):
        return "upscaler", None, None
    return "unknown", None, None


def classify_video(shapes):
    """Recognize video headers using native architecture signatures, never a filename guess."""
    names=set(shapes)
    audio={n.removeprefix('audio_vae.'):s for n,s in shapes.items()}
    if {'latents_mean','latents_std','dec_in_proj.weight'}<=audio.keys() and any(n.startswith('decoder.') for n in audio):
        return 'audio_vae',None,'minimax-audio-vae'
    tiny=shapes.get('decoder.1.weight')
    if tiny and len(tiny)==4 and tiny[1] in (16,24,32,48,128) and any('.conv.6.weight' in n or '.conv.0.weight' in n for n in names):
        return 'tae',None,'video-tae-'+str(tiny[1])
    if 'video_patch_proj.weight' in names and 'audio_patch_proj.weight' in names:
        return 'diffusion','minimax-h3','minimax-h3'
    if 'audio_patchify_proj.weight' in names and 'adaln_single.emb.timestep_embedder.linear_1.bias' in names:
        return 'diffusion','ltx2','ltx-2.5' if 'keyframes_abs_pos_embedding' in names else 'ltx-2.3'
    if 'txt_in.individual_token_refiner.blocks.0.adaLN_modulation.1.weight' in names and any(n.startswith('byt5_in.') for n in names):
        return 'diffusion','hunyuan-video-1.5','hunyuan-video-1.5'
    if 'patch_embedding.weight' in names and 'blocks.0.cross_attn.norm_k.weight' in names:
        shape=shapes['patch_embedding.weight']
        width=shape[0]
        channels=shape[1] if len(shape)==5 else None
        if any(n.startswith(('casual_audio_encoder.','audio_injector.')) for n in names):
            return 'diffusion','wan-s2v','s2v-deferred'
        if channels==48:
            return 'diffusion','wan2.2','ti2v-5b'
        variant='flf2v-14b' if 'img_emb.emb_pos' in names else 'i2v-14b' if any(n.startswith('img_emb.') for n in names) else 't2v-1.3b' if width==1536 else 't2v-14b'
        if any(n.startswith('vace_') or n.startswith('vace_blocks.') for n in names):
            variant='vace-1.3b' if width==1536 else 'vace-14b'
        return 'diffusion','wan2.1',variant
    # Standalone parts may carry their canonical loader prefix.
    parts={n.removeprefix('first_stage_model.'):s for n,s in shapes.items()}
    if 'decoder.x_embedder.weight' in parts and 'decoder.register_tokens' in parts:
        return 'vae',None,'minimax-video-vae'
    conv=parts.get('decoder.conv1.weight')
    if conv and len(conv) in (4,5):
        return 'vae',None,'wan2.2-vae' if conv[1]==48 else 'wan2.1-vae' if conv[1]==16 else 'unknown-video-vae'
    conv=parts.get('decoder.conv_in.conv.weight')
    if conv and len(conv)==5 and conv[1]==128:
        return 'vae',None,'ltx-conv-vae'
    conv=parts.get('decoder.conv_in.weight')
    if conv and len(conv)==5:
        if conv[1]==24:
            return 'vae',None,'minimax-video-vae'
        if conv[1]==32:
            return 'vae',None,'hunyuan-video-vae'
    if any(n.endswith('per_channel_statistics.std-of-means') for n in names):
        return 'vae',None,'ltx-diffusion-vae-unsupported'
    if any(n.startswith('audio_vae.') for n in names):
        return 'audio_vae',None,'ltx-audio-vae'
    if any(n.startswith(('vocoder.','audio_autoencoder.')) for n in names):
        return 'audio_vae',None,'minimax-audio-vae'
    shared=shapes.get('shared.weight') or shapes.get('encoder.embed_tokens.weight') or (
        shapes.get('token_embd.weight') if any(n.startswith('enc.blk.') for n in names) else None)
    if shared and shared[1]==4096 and shared[0]>=250000:
        return 'text_encoder',None,'umt5-xxl'
    if shared and shared[1]==1472 and shared[0]<=512:
        return 'text_encoder',None,'byt5-glyphxl'
    if 'vision_model.embeddings.patch_embedding.weight' in names and shapes['vision_model.embeddings.patch_embedding.weight'][0]==1280:
        return 'clip_vision',None,'clip-vision-h'
    embed=next((s for n,s in shapes.items() if n.endswith(('embed_tokens.weight','token_embd.weight'))),None)
    if embed and len(embed)==2 and embed[1]==3840:
        gemma4=any(n.endswith('.layer_scalar') for n in names)
        projected=any(n.startswith('text_embedding_projection.') for n in names)
        return 'text_encoder',None,'ltx-gemma4-12b' if gemma4 and projected else 'gemma4-stock-unsupported' if gemma4 else 'gemma3-12b'
    if embed and len(embed)==2 and embed[1]==5120 and any(n.startswith(('visual.','model.visual.')) for n in names):
        return 'text_encoder',None,'qwen3-vl-32b'
    if any(n.startswith('text_embedding_projection.') for n in names):
        return 'embeddings_connectors',None,'ltx-connector'
    if 'initial_norm.weight' in names and 'final_conv.weight' in names and any(n.startswith('res_blocks.') for n in names):
        return 'latent_upscaler',None,'ltx-latent-upscaler'
    return None


# Keep one bounded library scan plus its selected components without LRU churn.
@lru_cache(maxsize=2048)
def _identified(path, modified, size):
    """One file's identity, cached by its revision."""
    file = Path(path)
    if file.suffix.lower() == ".pth":
        return {"kind": "upscaler", "family": None, "variant": None, "label": "Image upscaler (.pth, ESRGAN family)"}
    try:
        if file.suffix.lower() == '.json':
            return identify_tokenizer(file)
        kind, family, variant = classify(shapes_of(file))
        if file.suffix.lower()=='.gguf' and kind=='text_encoder' and variant in ('qwen2.5-7b','qwen3-4b','qwen3-8b','llm-5120'):
            architecture=read_metadata(str(file)).get('general.architecture')
            if variant=='qwen2.5-7b' and architecture=='qwen2vl':
                variant='qwen2.5-vl-7b'
            elif variant=='qwen3-8b' and architecture=='qwen3vl':
                variant='qwen3-vl-8b'
            elif variant=='qwen3-4b' and architecture=='qwen3vl':
                variant='qwen3-vl-4b'
            elif variant=='llm-5120' and architecture=='qwen3vl':
                variant='qwen3-vl-32b'
    except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error):
        return {"kind": "unreadable", "family": None, "variant": None, "label": "Not a readable GGUF, safetensors or tokenizer JSON file"}
    label = LABELS.get(variant) or {"lora": "LoRA adapter", "upscaler": "Image upscaler", "flux1": "Flux 1 (not supported here)",
                                    "sd1": "Stable Diffusion 1.x/2.x (not supported here)"}.get(family or kind, kind.replace("_", " "))
    if kind == "lora":
        label = "LoRA for " + LABELS[variant] if variant else "LoRA adapter"
    return {"kind": kind, "family": family, "variant": variant, "label": label}


def identify_tokenizer(file):
    """Read bounded tokenizer data and distinguish Qwen3.5's vocabulary from earlier Qwen versions."""
    with file.open('rb') as stream:
        raw = stream.read(32 * 2**20 + 1)
    if len(raw) > 32 * 2**20:
        raise ValueError('Tokenizer JSON exceeds 32 MiB.')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('model'), dict) or not isinstance(data.get('added_tokens', []), list):
        raise ValueError('Invalid tokenizer JSON.')
    vocabulary = data.get('model', {})
    if not isinstance(vocabulary.get('vocab', {}), dict) or any(not isinstance(token, dict) for token in data.get('added_tokens', [])):
        raise ValueError('Invalid tokenizer vocabulary.')
    special = {token['content']: token['id'] for token in data.get('added_tokens', [])}
    matching = (vocabulary.get('type') == 'BPE' and len(vocabulary.get('vocab', {})) == 248044
                and all(special.get(name) == number for name, number in
                        (('<|endoftext|>', 248044), ('<|im_start|>', 248045), ('<|im_end|>', 248046))))
    return {'kind': 'tokenizer', 'family': None, 'variant': 'qwen3.5-tokenizer' if matching else None,
            'label': LABELS['qwen3.5-tokenizer'] if matching else 'Tokenizer JSON with an incompatible vocabulary'}


def identify(path, roots=None):
    """What one file is, or that there is no such file. With `roots`, a file outside those folders is not looked at."""
    file = Path(path)
    if roots is not None and not any(file.resolve().is_relative_to(Path(root).resolve()) for root in roots if root):
        return {"kind": "unreadable", "family": None, "variant": None, "label": "Outside this node's model folders"}
    try:
        stat = file.stat()
    except OSError:
        return {"kind": "missing", "family": None, "variant": None, "label": "No such file on this compute node"}
    if not file.is_file() or file.suffix.lower() not in FORMATS or file.suffix.lower() == '.json' and not tokenizer_asset(file):
        return {"kind": "unreadable", "family": None, "variant": None, "label": "Not a supported model or tokenizer data file"}
    return {**_identified(str(file), stat.st_mtime_ns, stat.st_size), "bytes": stat.st_size}


def scan(folder, roots=None):
    """Identify at most 2,000 library files, respecting the same roots as the selected parts."""
    root = Path(folder)
    if not folder or not root.is_dir():
        return []
    roots = [folder] if roots is None else roots
    files = islice((file for file in root.rglob("*") if (file.suffix.lower() in FORMATS and
                   (file.suffix.lower() != '.json' or tokenizer_asset(file))) and file.is_file()), 2000)
    return sorted([{"path": str(file), **identify(file, roots)} for file in files], key=lambda row: row["path"])


def needs(family, variant):
    """What each slot must hold: (required, expected kind, expected variant, why) by slot."""
    upscaler = (False, "upscaler", None, "Enlarges reference images smaller than the reference size, and outputs when asked to upscale.")
    from mirrorwall.video import FAMILIES
    if family in FAMILIES:
        def slot(kind,variant,why,required=True):
            """Keep the same slot contract for image and video component checklists."""
            return (required,kind,variant,why)
        channels=48 if variant=='ti2v-5b' else 16 if family.startswith('wan') else 128 if family=='ltx2' else 32 if family=='hunyuan-video-1.5' else 24
        tae={'tae':slot('tae','video-tae-'+str(channels),'Optional tiny video autoencoder; selecting it also changes native decoding.',False)}
        if family.startswith('wan'):
            result={'text_encoder':slot('text_encoder','umt5-xxl','WAN uses the umT5 XXL encoder.'),
                    'vae':slot('vae','wan2.2-vae' if variant=='ti2v-5b' else 'wan2.1-vae','Only TI2V 5B uses the Wan 2.2 VAE.'),
                    'high_noise_diffusion_model':slot('diffusion',variant,'A14B requires a compatible separate high-noise model.',family=='wan2.2' and variant!='ti2v-5b')}
            if family=='wan2.1' and variant in ('i2v-14b','flf2v-14b'):
                result['clip_vision']=slot('clip_vision','clip-vision-h','WAN 2.1 image conditioning requires CLIP Vision H.')
            return {**result,**tae}
        if family=='ltx2':
            return {**tae,'text_encoder':slot('text_encoder','ltx-gemma4-12b' if variant=='ltx-2.5' else 'gemma3-12b','LTX-2.5 needs its bundled projection, not stock Gemma 4.'),
                    'vae':slot('vae','ltx-conv-vae','Use the conv VAE; diffusion decoding is unsupported.'),
                    'audio_vae':slot('audio_vae','ltx-audio-vae','LTX audio decoder.'),
                    'embeddings_connectors':None if variant=='ltx-2.5' else slot('embeddings_connectors','ltx-connector','LTX-2.3 requires its embeddings connector.'),
                    'latent_upscaler':slot('latent_upscaler','ltx-latent-upscaler','Optional 2× latent enlargement.',False)}
        if family=='hunyuan-video-1.5':
            return {**tae,'text_encoder':slot('text_encoder','qwen2.5-vl-7b','Shared Qwen2.5-VL 7B image/video encoder.'),
                    'vae':slot('vae','hunyuan-video-vae','Use the causal HunyuanVideo 1.5 VAE.'),
                    'byt5':slot('text_encoder','byt5-glyphxl','Glyph-aware text conditioning needs ByT5 GlyphXL.')}
        return {**tae,'text_encoder':slot('text_encoder','qwen3-vl-32b','MiniMax uses its Qwen3-VL 32B encoder.'),
                'vae':slot('vae','minimax-video-vae','MiniMax video VAE.'),
                'audio_vae':slot('audio_vae','minimax-audio-vae','MiniMax stereo audio decoder.')}
    if family == 'moss-image':
        return {'text_encoder': (True, 'text_encoder', 'qwen3.5-9b', 'MOSS-Image Base and SFT use Qwen3.5 9B, not Qwen3 or Qwen-VL encoders.'),
                'vae': (True, 'vae', 'flux2-vae', 'MOSS-Image uses the Flux 2 VAE (32 latent channels).'),
                'tokenizer': (True, 'tokenizer', 'qwen3.5-tokenizer', 'Supply the external tokenizer/tokenizer.json from MOSS-Image; the embedded Qwen vocabulary is incompatible.'),
                'clip_l': None, 'clip_g': None, 'upscaler': upscaler}
    if family == 'krea2':
        return {'text_encoder': (True, 'text_encoder', 'qwen3-vl-4b', 'Krea 2 uses Qwen3-VL 4B, not the text-only Qwen3 4B used by Flux 2 klein.'),
                'vae': (True, 'vae', 'wan2.1-vae', 'Krea 2 decodes with the Wan 2.1 VAE (16 latent channels); the Comfy Krea 2 bundle ships it as qwen_image_vae.'),
                'clip_l': None, 'clip_g': None, 'upscaler': upscaler}
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
    control = (False, "controlnet", "sdxl-controlnet", "Optional: lets image workflows guide composition with a control image. "
               "Only an exclusive model entry may load one, since it adds memory.")
    if family == "sdxl" and variant == "checkpoint":
        return {"text_encoder": None, "clip_l": None, "clip_g": None, "upscaler": upscaler, "control_net": control,
                "vae": (False, "vae", "sdxl-vae", "The checkpoint has its own VAE; the fp16-fix VAE avoids black or noisy images at half precision.")}
    if family == "sdxl":
        return {"text_encoder": None, "upscaler": upscaler, "control_net": control,
                "vae": (True, "vae", "sdxl-vae", "A UNet-only file has no VAE of its own."),
                "clip_l": (True, "clip_l", "clip_l", "A UNet-only file has no text encoders of its own."),
                "clip_g": (True, "clip_g", "clip_g", "A UNet-only file has no text encoders of its own.")}
    return {"text_encoder": None, "vae": None, "clip_l": None, "clip_g": None, "upscaler": upscaler}


def check(settings, folder, roots=None, *, inventory=None):
    """Each slot of a draft image profile: what it holds, whether it fits the diffusion weights, and what to get or use instead.

    `folder` is searched for files that would fit a slot; `roots` are the only folders whose files are read.
    An authenticated `inventory` supplies header identities from several nodes without reading their paths here.
    """
    roots = [folder] if roots is None else roots
    options = settings.get("sdcpp", {}) if isinstance(settings, dict) else {}
    chosen = {"diffusion": (settings.get("flags") or {}).get("model", "") if isinstance(settings, dict) else "",
              **{key: value for key, value in (options.get("components") or {}).items() if isinstance(value, str)},
              "upscaler": options.get("upscaler") or ""}
    library = scan(folder, roots) if inventory is None else inventory
    known = {one['path']: one for one in library}
    def identity(path):
        """Remote inventories are authoritative; never interpret their paths on this host."""
        if inventory is None:
            return identify(path, roots)
        return known.get(path, {'kind':'missing', 'family':None, 'variant':None,
                                'label':'File is not available in the current model library'})
    diffusion = identity(chosen["diffusion"]) if chosen["diffusion"] else None
    family, variant = (diffusion or {}).get("family"), (diffusion or {}).get("variant")
    video=options.get('mode')=='vid_gen'
    if video and family=='wan2.1' and options.get('family')=='wan2.2' and options.get('components',{}).get('high_noise_diffusion_model'):
        family='wan2.2'  # A14B and 2.1 headers overlap; the explicit two-model bundle confirms the choice.
    wanted = needs(family, variant)
    slots, ready = [], True
    for slot, label in VIDEO_SLOTS if video else SLOTS:
        path = chosen.get(slot, "")
        found = identity(path) if path else None
        rule = (True, "diffusion", None, "The model itself.") if slot == "diffusion" else wanted.get(slot)
        entry = {"slot": slot, "label": label, "path": path, "found": found, "need": "unused" if rule is None else "required" if rule[0] else "optional",
                 "why": rule[3] if rule else ""}
        expected_kind, expected = (rule[1], rule[2]) if rule else (None, None)
        get = GET.get(expected or ("upscaler" if slot == "upscaler" else ""))
        if slot == "diffusion" and family in ("flux1", "sd1",'qwen-image-layered'):
            entry["status"], entry["message"] = "wrong", found["label"] + ": Mirror Wall runs Flux 2 klein, Flux 2 Dev and SDXL, plus Qwen Image, MOSS-Image and Krea 2 text-to-image profiles."
        elif slot == "diffusion" and found and found["kind"] == "diffusion" and not variant:
            entry["status"], entry["message"] = "wrong", "Diffusion weights of an unrecognized size; Mirror Wall knows Flux 2 klein 4B and 9B, Flux 2 Dev, SDXL, Qwen Image, Qwen Image 2.1, MOSS-Image and Krea 2."
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
        found = identity(entry.get("file", "")) if isinstance(entry, dict) else None
        fits = bool(found and found["kind"] == "lora" and (found["variant"] in (None, variant) or not variant))
        loras.append({"name": name, "found": found, "status": "ok" if fits else "wrong",
                      "message": found["label"] if fits else (found or {}).get("label", "Not a LoRA") + (f"; this model is {LABELS.get(variant, variant)}." if found and found["kind"] == "lora" else ".")})
        ready &= fits
    from mirrorwall.video import FAMILIES
    detected = family if family in (*FAMILIES,"flux2-klein", "flux2-dev", "sdxl",'qwen-image','qwen-image-2.1','moss-image','krea2') else None
    if detected and options.get("family") and options["family"] != detected:
        ready = False
    return {"detected_family": detected, "variant": variant, "variant_label": LABELS.get(variant, ""), "family_matches": not detected or options.get("family") == detected,
            "slots": slots, "loras": loras, "ready": bool(ready and diffusion and detected), "folder": str(folder or ""),
            "get_diffusion": [] if video else [{"label": LABELS[key], "repository": GET[key][0], "file": GET[key][1]} for key in ("klein-4b", "klein-9b", "dev", "checkpoint",'qwen-image','qwen-image-2.1','moss-image-base','moss-image-sft','krea2-raw','krea2-turbo')]}
