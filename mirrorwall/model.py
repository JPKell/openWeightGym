"""Image model/component settings and resource profiles, independent of chat flags."""
from copy import deepcopy
import math
from pathlib import Path
import re

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")

OUR_FLAGS = frozenset()
FAMILIES = ("flux2-klein", "flux2-dev", "sdxl", "qwen-image", "qwen-image-2.1")
SPLITS = ("single", "components", "layer", "row")
SAMPLERS = ("euler", "euler_a", "heun", "dpm2", "dpmpp2s_a", "dpmpp2m", "dpmpp2mv2", "lcm", "ipndm", "ipndm_v")
SCHEDULERS = ("flux2", "discrete", "karras", "exponential", "ays", "simple", "smoothstep", "sgm_uniform", "flux")


def path(value, label):
    """Require an explicit absolute asset path; existence is checked before loading."""
    if not isinstance(value,str) or not value or "\0" in value or not Path(value).is_absolute():
        raise ValueError(f"Image {label} must be an absolute path.")
    return value


def defaults(settings):
    """Resolve bounded workflow defaults without inheriting language-model defaults."""
    options=settings.get("sdcpp",{})
    dev=options.get("family")=="flux2-dev"
    sdxl=options.get("family")=="sdxl"
    qwen=options.get("family","").startswith('qwen-image')
    qwen21=options.get("family")=="qwen-image-2.1"
    return {"width":1024,"height":1024,"steps":50 if qwen else 28 if dev or sdxl else 6,
            "guidance":4.0 if dev else 1.0,"cfg_scale":6.0 if qwen21 else 4.0 if qwen else 7.0 if sdxl else 1.0,
            "sampler":"euler","scheduler":"flux" if qwen21 else "discrete" if sdxl or qwen else "flux2",
            "seed":-1,"n":1,"upscale":1,**options.get("defaults",{})}


def limits(settings):
    """Resolve per-profile limits that a request or saved preset cannot expand."""
    return {"max_width":2048,"max_height":2048,"max_pixels":2097152,
            "max_steps":100,"max_images":8,"max_references":0 if settings.get('sdcpp',{}).get('family','').startswith('qwen-image') else 1 if settings.get('sdcpp',{}).get('family')=='sdxl' else 2,
            "max_upscale":4 if settings.get('sdcpp',{}).get('upscaler') else 1,
            **settings.get("sdcpp",{}).get("limits",{})}


def check_name(name):
    """Explain the shared naming rule before consulting nodes or copying files."""
    if not isinstance(name,str) or not NAME.fullmatch(name):
        raise ValueError(
            "Invalid image model name. Use 1–128 characters, starting with A–Z, a–z or 0–9. "
            "Only letters, numbers, periods (.), underscores (_), colons (:), slashes (/) and "
            "hyphens (-) are allowed; spaces are not allowed. Example: qwen-image-2.1."
        )


def check_model(name,settings,cards):
    """Validate image assets, supported fields and explicit one/two-card layout."""
    check_name(name)
    if not isinstance(settings,dict) or settings.keys()-{"engine","flags","sdcpp","gpus","alone"}:
        raise ValueError("Image models accept engine, flags, sdcpp, gpus and alone.")
    if settings.get("engine")!="sdcpp" or not isinstance(settings.get("flags"),dict) or settings["flags"].keys()!={"model","ctx-size"} or settings["flags"]["ctx-size"]!=0:
        raise ValueError("Image models require engine=sdcpp, flags.model and ctx-size=0 (no language-token context).")
    path(settings["flags"]["model"],"diffusion model")
    options=settings.get("sdcpp")
    allowed={"family","components","split_mode","staged","host_ram_mib","defaults","limits",
             "reference_megapixels","upscaler","loras","threads","timeout","preview_interval","preparation_python","vram_limit_gib","layer_weights"}
    if not isinstance(options,dict) or options.keys()-allowed:
        raise ValueError("Unknown or missing sdcpp model options.")
    if options.get("family") not in FAMILIES or options.get("split_mode","single") not in SPLITS:
        raise ValueError("Choose a supported image family and split mode.")
    components=options.get("components",{})
    required={"vae","text_encoder"} if options["family"].startswith(("flux2","qwen-image")) else set()
    if not isinstance(components,dict) or not required<=components.keys() or components.keys()-{"vae","text_encoder","clip_l","clip_g"}:
        raise ValueError("Flux 2 and Qwen Image require explicit VAE and text-encoder components.")
    for key,value in components.items():
        path(value,key)
    for key in ("upscaler","preparation_python"):
        if options.get(key):
            path(options[key],key)
    if type(options.get("staged",True)) is not bool or type(settings.get("alone",False)) is not bool:
        raise ValueError("Image staged/alone settings must be boolean.")
    if 'layer_weights' in options:
        ratios=options['layer_weights']
        if options.get('split_mode')!='layer' or not isinstance(ratios,list) or len(ratios)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not .01<=v<=100 for v in ratios):
            raise ValueError('Layer weights require two positive ratios and the layer split mode.')
    budget=options.get("host_ram_mib",8192)
    if type(budget) is not int or not 2048<=budget<=131072:
        raise ValueError("Image host RAM budget must be 2048–131072 MiB.")
    count=1 if options.get("split_mode","single")=="single" else 2
    if "vram_limit_gib" in options:
        budgets=options["vram_limit_gib"]
        if not isinstance(budgets,list) or len(budgets)!=count or any(type(v) not in (int,float) or not math.isfinite(v) or not .5<=v<=128 for v in budgets):
            raise ValueError("VRAM limits require one positive GiB budget per assigned card.")
    wanted=settings.get("gpus",count)
    if wanted == 0 and options.get('split_mode', 'single') != 'single':
        raise ValueError('CPU image models require the single-device layout.')
    if not (type(wanted) is int and wanted in (0,count) or isinstance(wanted,list) and len(wanted)==count and len(set(wanted))==count and all(c in cards for c in wanted)):
        raise ValueError(f"This image layout requires exactly {count} enabled GPU(s).")
    if type(options.get("threads",4)) is not int or not 1<=options.get("threads",4)<=32:
        raise ValueError("Image threads must be 1–32.")
    for key,lo,hi,default in (("reference_megapixels",.1,4,1),("timeout",30,7200,1800),("preview_interval",1,50,1)):
        value=options.get(key,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:
            raise ValueError(f"Image {key} must be {lo}–{hi}.")
    if not isinstance(options.get("defaults",{}),dict) or not isinstance(options.get("limits",{}),dict):
        raise ValueError("Image defaults and limits must be objects.")
    maximum=limits(settings)
    if options.get("limits",{}).keys()-{"max_width","max_height","max_pixels","max_steps","max_images","max_references","max_upscale"}:
        raise ValueError("Unknown image limits.")
    for key,lo,hi in (("max_width",256,4096),("max_height",256,4096),("max_pixels",65536,16777216),
                      ("max_steps",1,200),("max_images",1,64),("max_references",0,8),("max_upscale",1,4)):
        value=maximum[key]
        if type(value) is not int or not lo<=value<=hi:
            raise ValueError("Invalid image limit "+key)
    if options["family"]=="sdxl" and maximum["max_references"]>1:
        raise ValueError("SDXL starts from one image (image-to-image); set max_references to 0 or 1.")
    if options['family'].startswith('qwen-image') and maximum['max_references']!=0:
        raise ValueError('Qwen Image profiles currently support text-to-image generation; set max_references to 0. Qwen Image Edit and vision conditioning require a separate profile integration.')
    configured=options.get("defaults",{})
    if configured.keys()-defaults({}).keys():
        raise ValueError("Unknown image default field.")
    from mirrorwall.requests import checked_request
    checked_request({"model":name,"prompt":"Validate image defaults",**defaults(settings)},settings)
    loras=options.get("loras",{})
    if not isinstance(loras,dict) or len(loras)>16:
        raise ValueError("Image LoRAs must be a map of at most 16 named assets.")
    for label,entry in loras.items():
        if not isinstance(label,str) or not NAME.fullmatch(label) or not isinstance(entry,dict) or entry.keys()-{"file","scale"}:
            raise ValueError("Invalid named image LoRA.")
        path(entry.get("file"),"LoRA")
        scale=entry.get("scale",1)
        if type(scale) not in (int,float) or not math.isfinite(scale) or not -4<=scale<=4:
            raise ValueError("LoRA scale must be -4–4.")


def effective(settings,default_flags):
    """Keep image configuration independent of global llama.cpp defaults."""
    return {**deepcopy(settings),"alone":settings.get('alone',False)}


def files_of(settings):
    """List every component/adapter/upscaler so Files protects the complete bundle."""
    options=settings.get("sdcpp",{})
    result={"model":settings["flags"]["model"],**options.get("components",{})}
    if options.get("upscaler"):
        result["upscaler"]=options["upscaler"]
    result.update({"lora:"+name:v["file"] for name,v in options.get("loras",{}).items()})
    return result


def file_selection(settings):
    """Return all image bundle file choices for revision tracking."""
    return tuple(files_of(settings).items())


def base(settings):
    """Image profiles do not share resident engines across different configurations."""
    return deepcopy(settings)


def kin_base(settings):
    """Keep every component, bound and placement in the resident identity."""
    return base(settings)


def fingerprint_settings(settings,hashes):
    """Replace filesystem locations with the bundle's content identities."""
    result=base(settings)
    result["flags"]["model"]=hashes["model"]
    result["sdcpp"]["components"]={k:hashes[k] for k in result["sdcpp"].get("components",{})}
    if result["sdcpp"].get("upscaler"):
        result["sdcpp"]["upscaler"]=hashes["upscaler"]
    for name,entry in result["sdcpp"].get("loras",{}).items():
        entry["file"]=hashes["lora:"+name]
    return result


def retarget(settings,target):
    """Apply relocated bundle paths while retaining workflow/resource settings."""
    result=base(settings)
    result["flags"]["model"]=target["flags"]["model"]
    for key in ("components","upscaler","loras"):
        if key in target.get("sdcpp",{}):
            result["sdcpp"][key]=deepcopy(target["sdcpp"][key])
    return result


def contexts(settings):
    """Image profiles have one bounded size envelope, no language context curve."""
    return [0]


def at_context(settings,context):
    """Retain the complete image size envelope during generic runner operations."""
    return base(settings)


def memory_base(settings):
    """Key measurements by complete model, workflow bounds and layout identity."""
    return base(settings)


def parallel_capacity(settings):
    """One admitted image job owns its runtime; batches generate sequentially."""
    return 1


def embedding(settings):
    """Image generators cannot serve embedding requests."""
    return False


def required_devices(settings,cards):
    """Return the explicitly configured one/two-card layout."""
    return settings.get("gpus",1 if settings["sdcpp"].get("split_mode","single")=="single" else 2)


def layouts(settings,cards):
    """Never silently change the requested image device count."""
    return [settings]


def fixed_split(settings,count):
    """Only pinned layer ratios or explicit component placement can share measured cards."""
    options=settings['sdcpp']
    return count==1 or options.get('split_mode')=='components' or bool(options.get('split_mode')=='layer' and options.get('layer_weights'))


def answered_settings(loaded,wanted):
    """Report the image profile that actually received admission."""
    return base(loaded)
