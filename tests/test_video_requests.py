"""Video bounds and runtime capability refusals before native inference."""
import base64
import io

import pytest
from PIL import Image

from mirrorwall import model, video
from mirrorwall.engine import SDCpp


def profile(family='wan2.1', **options):
    """A small explicit profile with observed native capability data."""
    return {'engine':'sdcpp','flags':{'model':'/models/video.gguf','ctx-size':0},'gpus':1,'alone':True,
            'sdcpp':{'mode':'vid_gen','family':family,'components':{'vae':'/models/vae.gguf','text_encoder':'/models/te.gguf'},
                'runtime_capabilities':{'supported_modes':['vid_gen'],'output_formats_by_mode':{'vid_gen':['webm']},
                                        'features_by_mode':{'vid_gen':{'init_image':True,'end_image':True,'lora':True}}},**options}}


def image():
    """Generate a bounded local inline input without external files."""
    output=io.BytesIO();Image.new('RGB',(256,256)).save(output,format='PNG')
    return base64.b64encode(output.getvalue()).decode()


def test_duration_and_native_translation():
    resolved=video.checked_request({'model':'wan','prompt':'Rain','seconds':'2.1','seed':42},profile())
    assert (resolved['frames'],resolved['fps'],resolved['seconds'])==(33,16,33/16)
    payload=video.native_payload(resolved,profile())
    assert payload['video_frames']==33 and payload['sample_params']['guidance']=={'txt_cfg':6.0}
    assert 'seconds' not in payload and 'frames' not in payload


@pytest.mark.parametrize('asked,why',[
    ({'seconds':float('nan')},'seconds'),({'fps':True},'fps'),({'width':817},'align'),
    ({'frames':1000},'frames'),({'sample_params':{}},'Unsupported'),({'init_image':'/etc/passwd'},'encoding'),
    ({'ref_audios':[]},'Unsupported'),({'output_format':'avi'},'WebM'),
    ({'end_image':image()},'start image'),({'reference_images':[image()]},'not supported'),
    ({'high_noise_steps':4},'not supported'),({'soundtrack':True},'soundtrack'),
    ({'loras':{'unknown':1}},'Unknown'),
])
def test_refusals(asked,why):
    with pytest.raises(ValueError,match=why):
        video.checked_request({'model':'wan','prompt':'Rain',**asked},profile())


def test_minimax_grid_and_limit_are_effective_not_requested():
    selected=profile('minimax-h3')
    resolved=video.checked_request({'model':'mini','prompt':'Rain'},selected)
    assert resolved['fps']==24 and resolved['frames']%17==5 and resolved['frames']/24<=5
    with pytest.raises(ValueError,match='Effective'):
        video.checked_request({'model':'mini','prompt':'Rain','seconds':5},selected)


def test_model_identity_routing_and_exclusive_placement():
    selected=profile()
    model.check_model('wan',selected,['CUDA0'])
    assert SDCpp().capabilities(selected)['video_generation']
    assert not SDCpp().capabilities(selected)['image_generation']
    with pytest.raises(ValueError,match='exclusive'):
        model.check_model('wan',{**selected,'alone':False},['CUDA0'])
    changed=profile(limits={'max_seconds':4})
    assert model.memory_base(selected)!=model.memory_base(changed)
    assert model.files_of(selected)['text_encoder']=='/models/te.gguf'


def test_account_limits_and_portrait_use_area_budget():
    account={'video_max_width':832,'video_max_height':480,'video_max_seconds':5}
    result=video.checked_request({'model':'wan','prompt':'Rain','size':'480x832'},profile(),account_limits=account)
    assert result['width']==480
    with pytest.raises(ValueError,match='pixel limit'):
        video.checked_request({'model':'wan','prompt':'Rain','size':'832x832'},profile(),account_limits=account)


def test_recipe_features_reject_unsupported_inputs_and_preserve_high_noise_loras():
    selected=profile(loras={'lightning':{'file':'/models/lightning.gguf','scale':.8,'is_high_noise':True}},
                     components={'vae':'/models/vae.gguf','text_encoder':'/models/te.gguf','high_noise_diffusion_model':'/models/high.gguf'})
    caps={'supported_modes':['vid_gen'],'output_formats_by_mode':{'vid_gen':['webm']},
          'generation_features':{'flow_shift':True},'features_by_mode':{'vid_gen':{'lora':True,'init_image':False}}}
    asked={'model':'wan','prompt':'Rain','flow_shift':5,'loras':{'lightning':.5}}
    resolved=video.checked_request(asked,selected,capabilities=caps)
    assert video.native_payload(resolved,selected)['lora']==[{'path':'/models/lightning.gguf','multiplier':.5,'is_high_noise':True}]
    with pytest.raises(ValueError,match='init_image'):
        video.checked_request({**asked,'init_image':image()},selected,capabilities=caps)


def test_video_usage_is_actual_clip_work_without_language_tokens():
    from mirrorwall.stream import Capture,encode
    capture=Capture('text/event-stream')
    capture.feed(encode({'type':'video_generation.completed','b64_json':'pixels','frame_count':33,'fps':16,
                         'seconds':33/16,'width':832,'height':480,'gpu_seconds':144}))
    assert capture.video_usage()=={'video_clips':1,'video_frames':33,'video_seconds':33/16,'video_pixels':832*480,'gpu_seconds':144}
    assert b'pixels' not in capture.snapshot()
    assert not any('token' in key for key in capture.video_usage())


def test_latent_upscale_uses_native_model_name_and_counts_output_bounds():
    selected=profile('ltx2',components={'vae':'/models/vae.gguf','text_encoder':'/models/te.gguf',
                                      'latent_upscaler':'/models/ltx-upscaler.safetensors'},
                     limits={'max_width':1280,'max_height':1280,'max_pixel_frames':1280*1280*241})
    caps={'supported_modes':['vid_gen'],'output_formats_by_mode':{'vid_gen':['webm']},
          'upscalers':[{'name':'ltx-upscaler','model':True,'image_upscale':False}]}
    resolved=video.checked_request({'model':'ltx','prompt':'Rain','width':512,'height':256,'hires':True},selected,capabilities=caps)
    assert video.native_payload(resolved,selected)['hires']['upscaler']=='ltx-upscaler'
    with pytest.raises(ValueError,match='upscale output'):
        video.checked_request({'model':'ltx','prompt':'Rain','width':512,'height':256,'hires':True},selected,
                              capabilities=caps,account_limits={'video_max_width':832,'video_max_height':480})


def test_the_runtime_admits_conditioning_before_it_has_loaded_its_capabilities():
    """Saved profiles hold no capabilities; the child checks a start image once its runtime reports them."""
    selected=profile()
    selected['sdcpp'].pop('runtime_capabilities')
    asked={'model':'wan','prompt':'Rain','init_image':image()}
    with pytest.raises(ValueError,match='Qualify the runtime capabilities'):
        video.checked_request(asked,selected)
    assert video.checked_request(asked,selected,deferred=True)['init_image']
    caps={'supported_modes':['vid_gen'],'output_formats_by_mode':{'vid_gen':['webm']},'features_by_mode':{'vid_gen':{'init_image':False}}}
    with pytest.raises(ValueError,match='init_image'):
        video.checked_request(asked,selected,capabilities=caps,deferred=True)  # the loaded runtime still decides


def test_the_runtime_child_generates_from_a_start_image_with_an_unqualified_saved_profile():
    """Live QA-108: the child also checked the request again, undeferred, to decide qualification."""
    from starlette.testclient import TestClient
    from mirrorwall.runtime import create_app
    selected=profile()
    caps=selected['sdcpp'].pop('runtime_capabilities')  # a saved profile holds none; the loaded runtime reports them

    class Answer:
        def raise_for_status(self): pass
        def json(self): return caps

    class Client:
        async def get(self,path): return Answer()

    class Native:
        client=Client(); payloads=[]
        async def start(self): pass
        async def stop(self): pass
        async def generate(self,payload):
            self.payloads.append(payload)
            yield {'type':'native.completed','native_seconds':1.0,'data':{'mime_type':'video/webm','output_format':'webm',
                   'frame_count':payload['video_frames'],'fps':payload['fps'],'b64_json':base64.b64encode(b'\x1aE\xdf\xa3'+bytes(64)).decode()}}

    native=Native()
    job={'settings':selected,'token':'test-private-credential','installation':{'executable':'/native/sd-server','cli':'/native/sd-cli'},'cards':['CUDA0']}
    with TestClient(create_app(job,native)) as client:
        answer=client.post('/v1/videos/generations',headers={'authorization':'Bearer '+job['token']},
                           json={'model':'wan','prompt':'Rain','seconds':1,'init_image':image(),'stream':True})
    assert answer.status_code==200 and 'event: error' not in answer.text, answer.text[-400:]
    assert 'video_generation.completed' in answer.text and native.payloads and native.payloads[0].get('init_image')
