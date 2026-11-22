"""Strict image request normalization; no arbitrary model paths or native arguments."""
import base64
import binascii
import math
import re
import json
import email
import email.policy

from mirrorwall import model

MAX_IMAGE_BYTES=20*1024*1024
MAX_REQUEST_BYTES=60*1024*1024


def request_body(content_type, body):
    """Accept JSON or the SDK's multipart edit form, retaining ordered image parts."""
    if len(body)>MAX_REQUEST_BYTES:
        raise ValueError("Image request exceeds 60 MiB.")
    if not content_type.lower().startswith('multipart/form-data'):
        value=json.loads(body)
        if not isinstance(value,dict):
            raise ValueError('Image request must be an object.')
        return value
    message=email.message_from_bytes(b'Content-Type: '+content_type.encode()+b'\r\n\r\n'+body,policy=email.policy.HTTP)
    if not message.is_multipart():
        raise ValueError('Invalid multipart image form.')
    value={}
    images=[]
    numbers={'n','steps','width','height','seed','guidance','cfg_scale','strength','upscale','partial_images','reference_megapixels'}
    for part in message.iter_parts():
        name=part.get_param('name',header='content-disposition')
        content=part.get_payload(decode=True)
        if name in ('image','image[]','mask'):
            encoded=encoded_image(base64.b64encode(content).decode())
            if name=='mask':
                if name in value:
                    raise ValueError('Use one edit mask.')
                value[name]=encoded
            else:
                images.append(encoded)
        else:
            if not name or name in value:
                raise ValueError('Repeated or missing image form field.')
            decoded=content.decode('utf-8')
            value[name]=json.loads(decoded) if name in numbers|{'stream','live_previews','loras','metadata','detail_resize'} else decoded
    if images:
        value['image']=images
    checked_request(value)
    return value


def encoded_image(value):
    """Accept bounded inline images only; remote fetches and host paths are forbidden."""
    if not isinstance(value,str):
        raise ValueError("Reference images must be base64 data URLs or base64 strings.")
    if value.startswith("data:"):
        if not re.match(r"data:image/(png|jpeg|webp);base64,",value):
            raise ValueError("Unsupported reference data URL.")
        value=value.split(",",1)[1]
    if len(value)>((MAX_IMAGE_BYTES+2)//3)*4:
        raise ValueError("A reference image exceeds 20 MiB.")
    try:
        decoded=base64.b64decode(value,validate=True)
    except (ValueError,binascii.Error):
        raise ValueError("Invalid reference image encoding.") from None
    if not decoded or len(decoded)>MAX_IMAGE_BYTES:
        raise ValueError("Invalid reference image size.")
    return value


def _number(value,label,lo,hi,integer=False):
    if (type(value) is not int if integer else type(value) not in (int,float)) or not math.isfinite(value) or not lo<=value<=hi:
        raise ValueError(f"{label} must be {'an integer' if integer else 'a number'} from {lo} to {hi}.")
    return value


def checked_advanced(value,result,settings,maximum):
    """Validate the allowlisted native sampling extensions; families decide which ones apply."""
    if not isinstance(value,dict):
        raise ValueError("advanced must be an object.")
    unknown=value.keys()-{"eta","flow_shift","shifted_timestep","img_cfg","slg","custom_sigmas","clip_skip","vae_tiling",
                          "cache_mode","cache_option","hires"}
    if unknown:
        raise ValueError("Unsupported advanced settings: "+", ".join(sorted(unknown)))
    family=(settings or {}).get("sdcpp",{}).get("family")
    sdxl=family=="sdxl"
    checked={}
    if "eta" in value:
        checked["eta"]=_number(value["eta"],"Eta",0,2)
    if "flow_shift" in value:
        if sdxl:
            raise ValueError("Flow shift applies to flow-matching models, not SDXL.")
        checked["flow_shift"]=_number(value["flow_shift"],"Flow shift",0,100)
    if "shifted_timestep" in value:
        if family and not sdxl:
            raise ValueError("Shifted timestep applies to SDXL models.")
        checked["shifted_timestep"]=_number(value["shifted_timestep"],"Shifted timestep",0,1000,True)
    if "img_cfg" in value:
        checked["img_cfg"]=_number(value["img_cfg"],"Image CFG scale",0,30)
    if "clip_skip" in value:
        if family and not sdxl:
            raise ValueError("CLIP skip applies to SDXL models.")
        checked["clip_skip"]=_number(value["clip_skip"],"CLIP skip",1,12,True)
    if "slg" in value:
        slg=value["slg"]
        if sdxl:
            raise ValueError("Skip-layer guidance applies to DiT models, not SDXL.")
        if not isinstance(slg,dict) or slg.keys()-{"scale","layer_start","layer_end","layers"}:
            raise ValueError("slg takes scale, layer_start, layer_end and layers.")
        checked["slg"]={"scale":_number(slg.get("scale",0),"Skip-layer guidance scale",0,10),
                        "layer_start":_number(slg.get("layer_start",.01),"Skip-layer start",0,1),
                        "layer_end":_number(slg.get("layer_end",.2),"Skip-layer end",0,1)}
        if checked["slg"]["layer_start"]>=checked["slg"]["layer_end"]:
            raise ValueError("Skip-layer start must be before its end.")
        if "layers" in slg:
            layers=slg["layers"]
            if not isinstance(layers,list) or not 1<=len(layers)<=16 or any(type(one) is not int or not 0<=one<=99 for one in layers):
                raise ValueError("Skip layers must list 1–16 layer numbers from 0 to 99.")
            checked["slg"]["layers"]=layers
    if "custom_sigmas" in value:
        sigmas=value["custom_sigmas"]
        if (not isinstance(sigmas,list) or len(sigmas)!=result["steps"]+1
                or any(type(one) not in (int,float) or not math.isfinite(one) or not 0<=one<=1000 for one in sigmas)
                or any(later>earlier for earlier,later in zip(sigmas,sigmas[1:]))):
            raise ValueError("Custom sigmas must list steps + 1 non-negative values from high to low.")
        checked["custom_sigmas"]=[float(one) for one in sigmas]
    if "vae_tiling" in value:
        if type(value["vae_tiling"]) is not bool:
            raise ValueError("vae_tiling must be true or false.")
        checked["vae_tiling"]=value["vae_tiling"]
    if "cache_option" in value and "cache_mode" not in value:
        raise ValueError("cache_option needs a cache_mode.")
    if "cache_mode" in value:
        mode=value["cache_mode"]
        if mode not in model.CACHE_MODES:
            raise ValueError("cache_mode must be one of: "+", ".join(model.CACHE_MODES)+".")
        if family and mode not in (model.UNET_CACHES if sdxl else model.DIT_CACHES):
            raise ValueError(f"The {mode} cache does not apply to this model family.")
        checked["cache_mode"]=mode
        option=value.get("cache_option","")
        if not isinstance(option,str) or not re.fullmatch(r"(?:[a-z_]+=-?[0-9.]+(?:,[a-z_]+=-?[0-9.]+){0,7})?",option):
            raise ValueError("cache_option must be comma-separated name=number pairs.")
        if option:
            checked["cache_option"]=option
    if "hires" in value:
        hires=value["hires"]
        if not isinstance(hires,dict) or hires.keys()-{"upscaler","scale","steps","denoising_strength"}:
            raise ValueError("hires takes upscaler, scale, steps and denoising_strength.")
        upscaler=hires.get("upscaler","Latent")
        if upscaler not in model.HIRES_UPSCALERS:
            raise ValueError("hires upscaler must be one of: "+", ".join(model.HIRES_UPSCALERS)+".")
        scale=_number(hires.get("scale",2),"Hires scale",1,4)
        width,height=round(result["width"]*scale/16)*16,round(result["height"]*scale/16)*16
        if width>maximum["max_width"] or height>maximum["max_height"] or width*height>maximum["max_pixels"]:
            raise ValueError("The hires pass would exceed this workflow's size limits.")
        checked["hires"]={"upscaler":upscaler,"target_width":width,"target_height":height,
                          "steps":_number(hires.get("steps",result["steps"]),"Hires steps",1,maximum["max_steps"],True),
                          "denoising_strength":_number(hires.get("denoising_strength",.45),"Hires denoising strength",0,1)}
    return checked


def native_extensions(resolved):
    """Native img_gen fields for a checked request's advanced settings."""
    advanced=resolved.get("advanced") or {}
    sample={key:advanced[key] for key in ("eta","flow_shift","shifted_timestep","custom_sigmas") if key in advanced}
    guidance={key:advanced[key] for key in ("img_cfg","slg") if key in advanced}
    top={}
    if "clip_skip" in advanced:
        top["clip_skip"]=advanced["clip_skip"]
    if advanced.get("vae_tiling"):
        top["vae_tiling_params"]={"enabled":True}
    if "cache_mode" in advanced:
        top.update(cache_mode=advanced["cache_mode"],cache_option=advanced.get("cache_option",""))
    if "hires" in advanced:
        top["hires"]={"enabled":True,**advanced["hires"]}
    return sample,guidance,top


def checked_control(asked,result,settings=None):
    """A ControlNet control image: base64 image, strength 0–2 and whether the runtime finds its edges first."""
    value=asked["control"]
    if not isinstance(value,dict) or value.keys()-{"image","strength","edges"} or "image" not in value:
        raise ValueError("control takes image, strength and edges.")
    strength=value.get("strength",.9)
    if type(strength) not in (int,float) or not math.isfinite(strength) or not 0<=strength<=2:
        raise ValueError("control.strength must be 0–2.")
    if type(value.get("edges",False)) is not bool:
        raise ValueError("control.edges must be true or false.")
    if "upscale_with" in asked or "detail_resize" in asked or "mask" in asked:
        raise ValueError("A control image cannot be combined with upscale_with, detail_resize or a mask.")
    if settings and not settings["sdcpp"].get("components",{}).get("control_net"):
        raise ValueError("This model has no ControlNet; add one to an exclusive SDXL model entry under Models → Images.")
    return {"image":encoded_image(value["image"]),"strength":strength,"edges":value.get("edges",False)}


def checked_upscale_with(asked,result):
    """An upscale-only request: one source image enlarged by a named installed upscaler file, then resized to the factor."""
    value=asked["upscale_with"]
    if not isinstance(value,dict) or value.keys()-{"upscaler","factor","tile"} or "upscaler" not in value:
        raise ValueError("upscale_with takes upscaler, factor and tile.")
    if not isinstance(value["upscaler"],str) or not model.UPSCALER_FILE.fullmatch(value["upscaler"]):
        raise ValueError("upscale_with.upscaler must name an installed upscaler file, such as 4x-UltraSharp.pth.")
    factor=value.get("factor",2)
    if type(factor) not in (int,float) or not math.isfinite(factor) or not 1<=factor<=4:
        raise ValueError("upscale_with.factor must be 1–4.")
    tile=value.get("tile",512)
    if type(tile) is not int or not 128<=tile<=1024:
        raise ValueError("upscale_with.tile must be an integer from 128 to 1024.")
    if len(result["references"])!=1 or "mask" in asked or result["n"]!=1 or result.get("upscale",1)!=1 or "detail_resize" in asked or asked.get("advanced"):
        raise ValueError("An upscale-only request takes one image and no mask, count, upscale factor, detail resize or advanced settings.")
    return {"upscaler":value["upscaler"],"factor":factor,"tile":tile}


def checked_request(asked,settings=None):
    """Normalize supported OpenAI fields and explicit openWeightGym image extensions."""
    if not isinstance(asked,dict):
        raise ValueError("Image request must be an object.")
    allowed={"model","prompt","negative_prompt","n","size","width","height","steps","seed","guidance","cfg_scale",
             "sampler","scheduler","references","image","mask","strength","loras","upscale","stream","partial_images",
             "response_format","output_format","user","quality","background","moderation","metadata","live_previews","reference_megapixels","detail_resize",
             "advanced","upscale_with","control"}
    unknown=asked.keys()-allowed
    if unknown:
        raise ValueError("Unsupported image settings: "+", ".join(sorted(unknown)))
    if not isinstance(asked.get("model"),str) or not asked["model"]:
        raise ValueError("Choose an image model.")
    prompt=asked.get("prompt")
    if "upscale_with" in asked and prompt is None:
        prompt=asked["prompt"]="Upscale"  # an upscale-only request generates nothing, so it needs no prompt
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>24000:
        raise ValueError("Prompt must contain 1–24000 characters.")
    result={**model.defaults(settings or {}),**asked}
    if "size" in asked:
        size=asked["size"]
        if not isinstance(size,str) or not re.fullmatch(r"\d{3,4}x\d{3,4}",size):
            raise ValueError("Use an explicit WIDTHxHEIGHT image size.")
        width,height=map(int,size.split("x"))
        if any(k in asked and asked[k]!=v for k,v in (("width",width),("height",height))):
            raise ValueError("size and width/height disagree.")
        result.update(width=width,height=height)
    result.pop("size",None)
    maximum=model.limits(settings) if settings else {"max_width":4096,"max_height":4096,"max_pixels":16777216,"max_steps":200,"max_images":64,"max_references":8}
    for key,lo,hi in (("width",256,maximum["max_width"]),("height",256,maximum["max_height"]),
                      ("steps",1,maximum["max_steps"]),("n",1,maximum["max_images"]),("seed",-1,2**31-1)):
        value=result[key]
        if type(value) is not int or not lo<=value<=hi:
            raise ValueError(f"{key} must be an integer from {lo} to {hi}.")
    alignment=32 if settings and settings.get('sdcpp',{}).get('family')=='qwen-image-2.1' else 16
    if result["width"]%alignment or result["height"]%alignment or result["width"]*result["height"]>maximum["max_pixels"]:
        raise ValueError(f"Dimensions must be multiples of {alignment} within the workflow's pixel limit.")
    for key,lo,hi,default in (("guidance",0,20,1),("cfg_scale",0,30,1),("strength",0,1,.75)):
        value=result.get(key,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:
            raise ValueError(f"Invalid {key}.")
        result[key]=value
    result["sampler"]=model.native_sampler(result["sampler"])
    if result["sampler"] not in model.SAMPLERS or result["scheduler"] not in model.SCHEDULERS:
        raise ValueError("Unsupported sampler or scheduler.")
    if "advanced" in asked:
        result["advanced"]=checked_advanced(asked["advanced"],result,settings,maximum)
    if type(result.get("stream",False)) is not bool:
        raise ValueError("stream must be true or false.")
    if type(result.get("partial_images",0)) is not int or not 0<=result.get("partial_images",0)<=3:
        raise ValueError("partial_images must be 0–3.")
    if type(result.get('live_previews',False)) is not bool:
        raise ValueError('live_previews must be true or false.')
    reference_mp=result.get('reference_megapixels',(settings or {}).get('sdcpp',{}).get('reference_megapixels',1))
    most_mp=(settings or {}).get('sdcpp',{}).get('reference_megapixels',4)
    if settings and settings['sdcpp']['family']=='sdxl':
        # An SDXL start image is encoded at the output size whatever it is prepared at, so preparing it at that size
        # (a refine or tile pass) costs no more memory than the output itself, which max_pixels already bounds.
        most_mp=max(most_mp,result['width']*result['height']/1_000_000)
    if type(reference_mp) not in (int,float) or not math.isfinite(reference_mp) or not .1<=reference_mp<=most_mp:
        raise ValueError('Reference size exceeds the workflow reference-size limit.')
    result['reference_megapixels']=reference_mp
    if result.get("response_format","b64_json")!="b64_json" or result.get("output_format","png")!="png":
        raise ValueError("This workflow returns PNG images as b64_json.")
    for key,permitted in (("quality",("auto",)),("background",("auto","opaque")),("moderation",("auto",))):
        if key in asked and asked[key] not in permitted:
            raise ValueError(f"Unsupported {key} setting.")
    negative=result.get("negative_prompt","")
    if not isinstance(negative,str) or len(negative)>24000:
        raise ValueError("Invalid negative prompt.")
    if negative and settings and settings["sdcpp"]["family"]!='sdxl' and not model.text_only(settings):
        raise ValueError("Negative prompts are not supported by this Flux workflow.")
    if 'strength' in asked and settings and settings['sdcpp']['family']!='sdxl':
        raise ValueError('Strength applies to SDXL image-to-image; Flux uses reference conditioning.')
    if settings and settings['sdcpp']['family']!='sdxl':
        result.pop('strength',None)
    refs=result.get("references",[])
    if "image" in asked:
        if refs:
            raise ValueError("Use image or references, not both.")
        refs=asked["image"] if isinstance(asked["image"],list) else [asked["image"]]
    # An upscale-only request's one source image is not a generation reference, so the model's reference limit
    # does not apply; checked_upscale_with holds it to exactly one (QA-090).
    if not isinstance(refs,list) or len(refs)>(max(1,maximum["max_references"]) if "upscale_with" in asked else maximum["max_references"]):
        raise ValueError("Too many reference images for this workflow.")
    result["references"]=[encoded_image(value) for value in refs]
    result.pop("image",None)
    if "mask" in result:
        if not result["references"]:
            raise ValueError("A mask requires a reference image.")
        result["mask"]=encoded_image(result["mask"])
    upscale=result.get("upscale",1)
    if type(upscale) not in (int,float) or not math.isfinite(upscale) or not 1<=upscale<=4:
        raise ValueError("Upscale factor must be 1–4.")
    if result["width"]*upscale>8192 or result["height"]*upscale>8192:
        raise ValueError("Upscaled dimensions exceed 8192 pixels.")
    if result['width']*result['height']*upscale**2>33554432:
        raise ValueError('Upscaled output exceeds 32 megapixels.')
    if settings and upscale>1 and not settings["sdcpp"].get("upscaler"):
        raise ValueError("This workflow has no configured output upscaler.")
    if settings and upscale>model.limits(settings)['max_upscale']:
        raise ValueError('Upscale exceeds this workflow limit.')
    if 'detail_resize' in asked:
        target=asked['detail_resize']
        if not isinstance(target,dict) or target.keys()!={'width','height'} or any(
                type(value) is not int or not 16<=value<=8192 for value in target.values()):
            raise ValueError('Detail resize requires integer width and height from 16 to 8192.')
        if target['width']*target['height']>33554432:
            raise ValueError('Detail resize exceeds 32 megapixels.')
        if upscale!=1 or 'mask' in asked or result['n']!=1:
            raise ValueError('Detail resize uses one image, an explicit final size, no output upscale factor and no mask.')
        largest=max(target['width']/result['width'],target['height']/result['height'])
        if largest>4:
            raise ValueError('Detail resize can enlarge the detail pass by at most 4×.')
        if settings:
            if settings['sdcpp']['family']!='sdxl' or not settings.get('alone',False):
                raise ValueError('Detail resize requires an exclusive SDXL image-to-image workflow.')
            if len(result['references'])!=1:
                raise ValueError('Detail resize requires one source image.')
            if largest>1 and not settings['sdcpp'].get('upscaler'):
                raise ValueError('Detail resize needs a configured AI upscaler for the requested final size.')
            if largest>model.limits(settings)['max_upscale']:
                raise ValueError('Detail resize exceeds this workflow upscale limit.')
    if settings and not settings.get('alone',False):
        # Sampler, scheduler and CFG only change the step arithmetic, not memory, except that any CFG other than 1 adds a
        # negative-prompt pass (stable-diffusion.cpp runs it when txt_cfg differs from img_cfg, which is 1 here). A profile
        # measured at CFG 1 has no room measured for that pass; one measured with it may use any CFG.
        if model.defaults(settings)['cfg_scale']==1 and result['cfg_scale']!=1:
            raise ValueError('This shared-residency model was measured at CFG 1, without a negative-prompt pass; a CFG other than 1 needs an exclusive model entry.')
        if result.get('advanced'):
            raise ValueError('Advanced sampling settings need an exclusive model entry; shared-residency workflows keep their measured profile.')
    if "upscale_with" in asked:
        result["upscale_with"]=checked_upscale_with(asked,result)
    if "control" in asked:
        result["control"]=checked_control(asked,result,settings)
    if result.get('advanced',{}).get('hires') and (result.get('detail_resize') or result.get('upscale',1)>1):
        raise ValueError('Use the hires pass or Upscale / Resize & add detail, not both.')
    loras=result.get("loras",{})
    if not isinstance(loras,dict) or len(loras)>16:
        raise ValueError("LoRAs must be named configured adapters and strengths.")
    for name,scale in loras.items():
        if settings and name not in settings["sdcpp"].get("loras",{}):
            raise ValueError("Unknown image LoRA: "+str(name))
        if type(scale) not in (int,float) or not math.isfinite(scale) or not -4<=scale<=4:
            raise ValueError("LoRA strength must be -4–4.")
    if "metadata" in result and (not isinstance(result["metadata"],dict) or len(str(result["metadata"]))>4000):
        raise ValueError("Invalid image metadata.")
    return result
