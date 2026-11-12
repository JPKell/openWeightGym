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
            value[name]=json.loads(decoded) if name in numbers|{'stream','live_previews','loras','metadata'} else decoded
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


def checked_request(asked,settings=None):
    """Normalize supported OpenAI fields and explicit openWeightGym image extensions."""
    if not isinstance(asked,dict):
        raise ValueError("Image request must be an object.")
    allowed={"model","prompt","negative_prompt","n","size","width","height","steps","seed","guidance","cfg_scale",
             "sampler","scheduler","references","image","mask","strength","loras","upscale","stream","partial_images",
             "response_format","output_format","user","quality","background","moderation","metadata","live_previews","reference_megapixels"}
    unknown=asked.keys()-allowed
    if unknown:
        raise ValueError("Unsupported image settings: "+", ".join(sorted(unknown)))
    if not isinstance(asked.get("model"),str) or not asked["model"]:
        raise ValueError("Choose an image model.")
    prompt=asked.get("prompt")
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
    if result["sampler"] not in model.SAMPLERS or result["scheduler"] not in model.SCHEDULERS:
        raise ValueError("Unsupported sampler or scheduler.")
    if type(result.get("stream",False)) is not bool:
        raise ValueError("stream must be true or false.")
    if type(result.get("partial_images",0)) is not int or not 0<=result.get("partial_images",0)<=3:
        raise ValueError("partial_images must be 0–3.")
    if type(result.get('live_previews',False)) is not bool:
        raise ValueError('live_previews must be true or false.')
    reference_mp=result.get('reference_megapixels',(settings or {}).get('sdcpp',{}).get('reference_megapixels',1))
    if type(reference_mp) not in (int,float) or not math.isfinite(reference_mp) or not .1<=reference_mp<=(settings or {}).get('sdcpp',{}).get('reference_megapixels',4):
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
    if negative and settings and settings["sdcpp"]["family"] not in ('sdxl','qwen-image','qwen-image-2.1'):
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
    if not isinstance(refs,list) or len(refs)>maximum["max_references"]:
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
    if settings and not settings.get('alone',False):
        ordinary=model.defaults(settings)
        if any(result[key]!=ordinary[key] for key in ('sampler','scheduler','cfg_scale')):
            raise ValueError('A shared-residency workflow pins its sampler, scheduler and CFG scale; edit the profile or use an exclusive model entry.')
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
