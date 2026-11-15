"""Bounded local video profiles and requests; native paths and arguments stay private."""
import math
import re

from mirrorwall import model
from mirrorwall.requests import encoded_image

FAMILIES=('wan2.1','wan2.2','ltx2','hunyuan-video-1.5','minimax-h3')
COMPONENTS=('vae','text_encoder','high_noise_diffusion_model','audio_vae','embeddings_connectors',
            'byt5','clip_vision','latent_upscaler','tae')
RECIPES=('wan21-t2v','wan21-i2v','wan21-flf2v','wan21-vace','wan22-ti2v-5b','wan22-t2v','wan22-i2v',
         'ltx23-dev','ltx23-distilled','ltx25-dev','hunyuan-video-15-t2v','minimax-h3-fl2va','minimax-h3-ref2va')
WAN_NEGATIVE='色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走'


def is_video(settings):
    """Media mode is explicit so a video profile never satisfies an image or chat route."""
    return settings.get('sdcpp',{}).get('mode')=='vid_gen'


def defaults(settings):
    """Family starting values; saved defaults remain part of the profile identity."""
    options=settings.get('sdcpp',{})
    family=options.get('family','wan2.1')
    return {'width':832,'height':480,'seconds':4.4 if family=='minimax-h3' else 5,
            'fps':24 if family in ('minimax-h3','ltx2') else 16,'steps':20,
            'guidance':1.0 if family=='minimax-h3' else 3.0 if family=='ltx2' else 6.0,
            'sampler':'euler','scheduler':'discrete','seed':-1,
            'negative_prompt':WAN_NEGATIVE if family.startswith('wan') else '',
            **options.get('defaults',{})}


def limits(settings):
    """Hard per-profile duration, frame, pixel-frame and wall-clock bounds."""
    return {'max_width':832,'max_height':832,'max_seconds':5,'max_frames':241,
            'max_pixel_frames':832*480*241,'max_steps':100,'max_references':8,'max_job_seconds':1800,
            **settings.get('sdcpp',{}).get('limits',{})}


def number(value, name, minimum, maximum, *, integer=False):
    """Reject booleans, NaN, infinities and unbounded JSON numbers."""
    if type(value) not in ((int,) if integer else (int,float)) or not math.isfinite(value) or not minimum<=value<=maximum:
        raise ValueError(f'{name} must be {"an integer" if integer else "a number"} from {minimum} to {maximum}.')
    return value


def frame_count(target, family):
    """Mirror native normalization: ordinary 4n+1 downward, MiniMax 17k+5 upward."""
    number(target,'frames',5,601,integer=True)
    if family=='minimax-h3':
        return 5+17*math.ceil((target-5)/17)
    return 1+4*((target-1)//4)


def features(settings, capabilities=None):
    """Use loaded-runtime evidence, never infer available inputs from a family table."""
    value=capabilities if capabilities is not None else settings.get('sdcpp',{}).get('runtime_capabilities',{})
    if 'vid_gen' not in value.get('supported_modes',[]):
        return {}
    result={**value.get('generation_features',{}),**value.get('features_by_mode',{}).get('vid_gen',{})}
    result['hires']=any(one.get('model') and not one.get('image_upscale') for one in value.get('upscalers',[]))
    return result


def recipe(settings):
    """Select an upstream recipe; feature declarations remain owned by the loaded native server."""
    from mirrorwall.components import identify
    options=settings['sdcpp']
    if options.get('recipe'):
        return options['recipe']
    family=options['family']
    variant=identify(settings['flags']['model']).get('variant') or ''
    if family.startswith('wan'):
        prefix='wan22' if family=='wan2.2' else 'wan21'
        mode='ti2v-5b' if variant=='ti2v-5b' else 'vace' if variant.startswith('vace') else 'flf2v' if variant.startswith('flf2v') else 'i2v' if variant.startswith('i2v') else 't2v'
        return ('wan21' if mode=='vace' else prefix)+'-'+mode
    return 'ltx25-dev' if variant=='ltx-2.5' else 'ltx23-dev' if family=='ltx2' else 'hunyuan-video-15-t2v' if family=='hunyuan-video-1.5' else 'minimax-h3-fl2va'


def checked_request(asked, settings=None, *, capabilities=None, account_limits=None):
    """Normalize the local Videos API and refuse every unsupported extension explicitly."""
    if not isinstance(asked,dict):
        raise ValueError('Video request must be an object.')
    allowed={'model','prompt','negative_prompt','size','width','height','seconds','fps','frames','seed','steps',
             'guidance','sampler','scheduler','flow_shift','high_noise_steps','high_noise_guidance','moe_boundary',
             'input_reference','init_image','end_image','reference_images','control_video','vace_strength',
             'loras','soundtrack','vae_tiling','temporal_tiling','hires','output_format','metadata','stream','live_previews'}
    if asked.keys()-allowed:
        raise ValueError('Unsupported video settings: '+', '.join(sorted(asked.keys()-allowed)))
    if not isinstance(asked.get('model'),str) or not asked['model']:
        raise ValueError('Choose a video model.')
    if not isinstance(asked.get('prompt'),str) or not 1<=len(asked['prompt'].strip())<=24000:
        raise ValueError('Prompt must contain 1–24000 characters.')
    if settings is not None and not is_video(settings):
        raise ValueError('Choose a video-generation profile.')
    has_profile=settings is not None
    settings=settings or {}
    options=settings.get('sdcpp',{})
    family=options.get('family','wan2.1')
    maximum=limits(settings)
    if not has_profile:
        maximum={**maximum,'max_width':1280,'max_height':1280,'max_seconds':10,'max_frames':601,'max_pixel_frames':1280*1280*601,'max_steps':200}
    result={**defaults(settings),**asked}
    if 'size' in asked:
        if not isinstance(asked['size'],str) or not re.fullmatch(r'\d{3,4}x\d{3,4}',asked['size']):
            raise ValueError('Use an explicit WIDTHxHEIGHT video size.')
        w,h=map(int,asked['size'].split('x'))
        if any(key in asked and asked[key]!=value for key,value in [('width',w),('height',h)]):
            raise ValueError('size and width/height disagree.')
        result.update(width=w,height=h)
    result.pop('size',None)
    policy=account_limits or {}
    for key in ('width','height'):
        number(result[key],key,256,min(maximum['max_'+key],max(policy.get('video_max_width',1280),policy.get('video_max_height',1280))),integer=True)
        if result[key]%(32 if family=='minimax-h3' else 16):
            raise ValueError('Video dimensions must align to '+('32' if family=='minimax-h3' else '16')+' pixels.')
    # Portrait presets use the same maximum side and area budget as landscape.
    if result['width']*result['height']>policy.get('video_max_width',1280)*policy.get('video_max_height',1280):
        raise ValueError('Video dimensions exceed the account pixel limit.')
    for key,lo,hi in [('fps',1,60),('steps',1,maximum['max_steps']),('seed',-1,2**31-1)]:
        number(result[key],key,lo,hi,integer=True)
    if family=='minimax-h3' and result['fps']!=24:
        raise ValueError('MiniMax-H3 requires 24 fps.')
    seconds=result['seconds']
    if isinstance(seconds,str) and re.fullmatch(r'\d+(?:\.\d+)?',seconds):
        seconds=float(seconds)
    number(seconds,'seconds',.1,min(maximum['max_seconds'],policy.get('video_max_seconds',10)))
    target=asked.get('frames',math.floor(seconds*result['fps']))
    frames=frame_count(target,family)
    effective=frames/result['fps']
    if frames>maximum['max_frames'] or effective>min(maximum['max_seconds'],policy.get('video_max_seconds',10)):
        raise ValueError('Effective frame count or length exceeds the profile/account limit; reduce the length.')
    if 'frames' in asked and 'seconds' in asked and frames!=frame_count(math.floor(seconds*result['fps']),family):
        raise ValueError('frames and seconds disagree after normalization.')
    if frames*result['width']*result['height']>maximum['max_pixel_frames']:
        raise ValueError('Video exceeds the profile pixel × frame budget.')
    result.update(frames=frames,seconds=effective)
    for key,lo,hi in [('guidance',0,30),('flow_shift',0,100),('high_noise_guidance',0,30),('moe_boundary',0,1),('vace_strength',0,2)]:
        if key in result:
            number(result[key],key,lo,hi)
    if 'high_noise_steps' in result:
        number(result['high_noise_steps'],'high_noise_steps',1,maximum['max_steps'],integer=True)
    if result['sampler'] not in model.SAMPLERS or result['scheduler'] not in model.SCHEDULERS:
        raise ValueError('Unsupported video sampler or scheduler.')
    if not isinstance(result['negative_prompt'],str) or len(result['negative_prompt'])>24000:
        raise ValueError('Invalid negative prompt.')
    for key in ('soundtrack','vae_tiling','temporal_tiling','hires','stream','live_previews'):
        if key in result and type(result[key]) is not bool:
            raise ValueError(key+' must be true or false.')
    for key in ('vae_tiling','temporal_tiling'):
        if options.get(key) and result.get(key) is False:
            raise ValueError('This profile requires '+key+' for its measured memory bound.')
    if result.get('output_format','webm')!='webm':
        raise ValueError('The Videos API returns WebM; animated WebP is not a video player container.')
    if 'input_reference' in result:
        if 'init_image' in result:
            raise ValueError('Use input_reference or init_image, not both.')
        result['init_image']=result.pop('input_reference')
    for key in ('init_image','end_image'):
        if key in result:
            result[key]=encoded_image(result[key])
    for key in ('reference_images','control_video'):
        if key in result:
            bound=maximum['max_references'] if key=='reference_images' else maximum['max_frames']
            if not isinstance(result[key],list) or not 1<=len(result[key])<=bound:
                raise ValueError(key+' must be a bounded nonempty list of image frames.')
            result[key]=[encoded_image(one) for one in result[key]]
    if result.get('end_image') and not result.get('init_image'):
        raise ValueError('An end image requires a start image.')
    if result.get('reference_images') and any(result.get(key) for key in ('init_image','end_image','control_video')):
        raise ValueError('Reference images cannot be combined with start/end images or control video.')
    if has_profile and result.get('reference_images') and family!='minimax-h3':
        raise ValueError('Reference images are not supported by this native video family; use a qualified start image or control mode.')
    if result.get('control_video') and len(result['control_video'])!=frames:
        raise ValueError('Control video must contain exactly the effective number of frames.')
    supported=features(settings,capabilities)
    evidence=capabilities is not None or bool(options.get('runtime_capabilities'))
    if evidence:
        caps=capabilities if capabilities is not None else options['runtime_capabilities']
        if 'vid_gen' not in caps.get('supported_modes',[]) or 'webm' not in caps.get('output_formats_by_mode',{}).get('vid_gen',[]):
            raise ValueError('The loaded runtime does not support video with WebM output.')
        requirements={'init_image':'init_image','end_image':'end_image','reference_images':'ref_images','control_video':'control_frames',
                      'vace_strength':'control_frames','high_noise_steps':'high_noise_sample_params',
                      'high_noise_guidance':'high_noise_sample_params','moe_boundary':'high_noise_sample_params',
                      'vae_tiling':'vae_tiling','temporal_tiling':'vae_tiling','flow_shift':'flow_shift','hires':'hires','live_previews':'preview'}
        for key,feature in requirements.items():
            if key in result and result.get(key) is not False and not supported.get(feature):
                raise ValueError(key+' is not supported by this qualified runtime profile.')
        if has_profile and any(key in result for key in ('high_noise_steps','high_noise_guidance','moe_boundary')) and not options.get('components',{}).get('high_noise_diffusion_model'):
            raise ValueError('High-noise controls are not supported without a high-noise diffusion component.')
    elif has_profile and any(key in asked for key in ('init_image','input_reference','end_image','reference_images','control_video','high_noise_steps','high_noise_guidance','moe_boundary','hires')):
        raise ValueError('Qualify the runtime capabilities before using video conditioning or advanced modes.')
    if has_profile and result.get('soundtrack') and not options.get('components',{}).get('audio_vae'):
        raise ValueError('This profile has no qualified soundtrack components.')
    if has_profile and result.get('hires') and not options.get('components',{}).get('latent_upscaler'):
        raise ValueError('This profile has no latent upscaler.')
    if result.get('hires'):
        if result['width']*2>min(maximum['max_width'],max(policy.get('video_max_width',1280),policy.get('video_max_height',1280))) or result['height']*2>min(maximum['max_height'],max(policy.get('video_max_width',1280),policy.get('video_max_height',1280))) or result['width']*result['height']*4>policy.get('video_max_width',1280)*policy.get('video_max_height',1280) or result['width']*result['height']*4*frames>maximum['max_pixel_frames']:
            raise ValueError('Latent upscale output exceeds the profile dimensions or pixel × frame budget.')
    loras=result.get('loras',{})
    if not isinstance(loras,dict) or len(loras)>16:
        raise ValueError('LoRAs must name configured adapters and strengths.')
    for name,strength in loras.items():
        if options and name not in options.get('loras',{}):
            raise ValueError('Unknown video LoRA.')
        number(strength,'LoRA strength',-4,4)
    if has_profile and any(entry.get('is_high_noise') and loras.get(name,entry.get('scale',1)) for name,entry in options.get('loras',{}).items()) and not options.get('components',{}).get('high_noise_diffusion_model'):
        raise ValueError('High-noise LoRAs require a high-noise diffusion component.')
    if loras and evidence and not supported.get('lora'):
        raise ValueError('This runtime profile does not support LoRAs.')
    if 'metadata' in result and (not isinstance(result['metadata'],dict) or len(str(result['metadata']))>4000):
        raise ValueError('Invalid video metadata.')
    return result


def check_profile(name, settings, cards):
    """Validate video-specific fields while reusing the existing engine placement contract."""
    options=settings.get('sdcpp',{})
    if options.get('family') not in FAMILIES or not is_video(settings):
        raise ValueError('Video profiles require mode=vid_gen and a supported video family.')
    if settings.get('alone') is not True or settings.get('gpus')==0:
        raise ValueError('Video profiles require exclusive GPU admission.')
    allowed={'family','mode','components','split_mode','staged','host_ram_mib','defaults','limits','loras',
             'threads','timeout','preview_interval','vram_limit_gib','layer_weights','runtime_capabilities','variant',
             'vae_tiling','temporal_tiling','recipe'}
    if options.get('recipe') and options['recipe'] not in RECIPES:
        raise ValueError('Choose a supported upstream video recipe.')
    if options.keys()-allowed:
        raise ValueError('Unknown video profile options: '+', '.join(sorted(options.keys()-allowed)))
    components=options.get('components',{})
    if not isinstance(components,dict) or components.keys()-set(COMPONENTS) or not {'vae','text_encoder'}<=components.keys():
        raise ValueError('Video requires VAE and text encoder, with only supported component slots.')
    for key,value in components.items():
        model.path(value,key)
    for key in ('vae_tiling','temporal_tiling'):
        if key in options and type(options[key]) is not bool:
            raise ValueError(key+' must be true or false.')
    # Placement, budgets, adapters and flags have the same contract as image profiles.
    surrogate={**settings,'sdcpp':{key:value for key,value in options.items() if key in
        ('split_mode','staged','host_ram_mib','loras','threads','timeout','preview_interval','vram_limit_gib','layer_weights')}}
    surrogate['sdcpp'].update(family='sdxl',components={},limits={'max_references':0})
    loras=options.get('loras',{})
    if not isinstance(loras,dict):
        raise ValueError('Video LoRAs must be configured named adapters.')
    for entry in loras.values():
        if not isinstance(entry,dict) or entry.keys()-{'file','scale','is_high_noise'} or type(entry.get('is_high_noise',False)) is not bool:
            raise ValueError('Video adapters accept file, scale and a boolean is_high_noise.')
    surrogate['sdcpp']['loras']={name:{key:value for key,value in entry.items() if key!='is_high_noise'} for name,entry in loras.items()}
    model.check_model(name,surrogate,cards)
    if not isinstance(options.get('limits',{}),dict) or options.get('limits',{}).keys()-limits({}).keys():
        raise ValueError('Unknown video limits.')
    for key,lo,hi in [('max_width',256,1280),('max_height',256,1280),('max_seconds',.1,10),('max_frames',5,601),
                      ('max_pixel_frames',256*256*5,1280*1280*601),('max_steps',1,200),('max_references',0,8),('max_job_seconds',30,7200)]:
        number(limits(settings)[key],key,lo,hi,integer=key!='max_seconds')
    if not isinstance(options.get('defaults',{}),dict) or options.get('defaults',{}).keys()-defaults({}).keys():
        raise ValueError('Unknown video default field.')
    checked_request({'model':name,'prompt':'Validate video defaults',**defaults(settings)},settings)


def native_payload(resolved, settings):
    """Translate normalized public fields into an allowlisted native video request."""
    sample={'sample_steps':resolved['steps'],'sample_method':resolved['sampler'].replace('dpmpp','dpm++'),
            'scheduler':resolved['scheduler'],'guidance':{'txt_cfg':resolved['guidance']}}
    if 'flow_shift' in resolved:
        sample['flow_shift']=resolved['flow_shift']
    result={key:resolved[key] for key in ('prompt','negative_prompt','width','height','seed','fps')}
    result.update(video_frames=resolved['frames'],sample_params=sample,output_format='webm',preview='proj' if resolved.get('live_previews') else 'none')
    for public,native in [('init_image','init_image'),('end_image','end_image'),('reference_images','ref_images'),('control_video','control_frames'),('vace_strength','vace_strength'),('moe_boundary','moe_boundary')]:
        if public in resolved:
            result[native]=resolved[public]
    if 'high_noise_steps' in resolved or 'high_noise_guidance' in resolved:
        result['high_noise_sample_params']={**sample,'sample_steps':resolved.get('high_noise_steps',resolved['steps']),
                                           'guidance':{'txt_cfg':resolved.get('high_noise_guidance',resolved['guidance'])}}
    options=settings['sdcpp']
    if resolved.get('hires'):
        from pathlib import Path
        result['hires']={'enabled':True,'scale':2,'upscaler':Path(options['components']['latent_upscaler']).stem}
    if resolved.get('vae_tiling',options.get('vae_tiling',False)) or resolved.get('temporal_tiling',options.get('temporal_tiling',False)):
        result['vae_tiling_params']={'enabled':True,'temporal_tiling':resolved.get('temporal_tiling',options.get('temporal_tiling',False))}
    result['lora']=[{'path':entry['file'],'multiplier':resolved.get('loras',{}).get(name,entry.get('scale',1)),
                     'is_high_noise':entry.get('is_high_noise',False)}
                    for name,entry in options.get('loras',{}).items() if resolved.get('loras',{}).get(name,entry.get('scale',1))]
    return result
