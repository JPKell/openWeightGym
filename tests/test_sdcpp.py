"""Image configuration, request boundaries and optional installer contracts without GPU work."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
from starlette.testclient import TestClient

from engines import engine_for,checked_installations
from mirrorwall import model
from mirrorwall.requests import checked_request
from mirrorwall.runtime import create_app,native_command,png_bytes

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("sdcpp_installer",ROOT/"mirrorwall/install.py")
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


@pytest.mark.parametrize('dimensions,mp',[((128,96),.5),((4032,3024),1),((96,128),.5),((1024,1024),1)])
def test_reference_geometry_is_native_packable_and_a_resize_fixed_point(dimensions,mp):
    from mirrorwall.runtime import reference_size
    target=reference_size(*dimensions,mp)
    assert all(value%16==0 for value in target)
    assert reference_size(*target,mp)==target


@pytest.fixture
def image_settings(tmp_path):
    assets={name:str(tmp_path/(name+".gguf")) for name in ("diffusion","text_encoder","vae")}
    for value in assets.values(): Path(value).write_bytes(b"test asset")
    return {"engine":"sdcpp","flags":{"model":assets.pop("diffusion"),"ctx-size":0},
            "gpus":2,"alone":True,"sdcpp":{"family":"flux2-dev","components":assets,
                "split_mode":"layer","staged":True,"host_ram_mib":8192}}


def test_image_profile_has_explicit_two_card_capacity_and_complete_file_identity(image_settings):
    model.check_model("flux2-dev",image_settings,("CUDA0","CUDA1"))
    assert model.required_devices(image_settings,("CUDA0","CUDA1"))==2
    assert len(model.files_of(image_settings))==3
    assert model.contexts(image_settings)==[0]
    assert model.effective(image_settings,{"ctx-size":1234})["flags"]["ctx-size"]==0
    bad={**image_settings,"gpus":1}
    with pytest.raises(ValueError,match="exactly 2"): model.check_model("flux2-dev",bad,("CUDA0","CUDA1"))


def test_image_requests_keep_workflow_defaults_and_do_not_enter_chat(image_settings):
    engine=engine_for("sdcpp")
    asked={"model":"flux2-dev","prompt":"A red boat"}
    assert engine.prepare_request("/v1/images/generations",asked)==asked
    normalized=checked_request(asked,image_settings)
    assert normalized["steps"]==28 and normalized["guidance"]==4
    with pytest.raises(ValueError,match="image generation"): engine.check_request("/v1/chat/completions",asked)
    assert not engine.capabilities(image_settings)["chat"]
    assert engine.capabilities(image_settings)["image_streaming"]


@pytest.mark.parametrize("extra",[
    {"width":True},{"size":"1024x1024","width":512},{"sampler":"shell command"},
    {"guidance":float("nan")},{"sd_cpp_extra_args":{"backend":"cpu"}},
    {"references":["https://example.com/private.png"]},{"references":["/etc/passwd"]},
    {"mask":"eA=="},{"loras":{"/arbitrary/path":1}},{"negative_prompt":"ignored"},{"strength":.5},
])
def test_unsupported_settings_fail_clearly_before_generation(image_settings,extra):
    with pytest.raises(ValueError): checked_request({"model":"flux2-dev","prompt":"A boat",**extra},image_settings)


def test_image_limits_and_component_changes_are_in_the_measurement_identity(image_settings):
    before=model.memory_base(image_settings)
    image_settings["sdcpp"]["limits"]={"max_pixels":1048576,"not_a_limit":1}
    with pytest.raises(ValueError,match="Unknown image limits"): model.check_model("flux",image_settings,("CUDA0","CUDA1"))
    image_settings["sdcpp"]["limits"].pop("not_a_limit")
    assert before!=model.memory_base(image_settings)
    hashes={key:"sha-"+key for key in model.files_of(image_settings)}
    identified=model.fingerprint_settings(image_settings,hashes)
    assert identified["sdcpp"]["components"]["text_encoder"]=="sha-text_encoder"
    assert image_settings["sdcpp"]["components"]["text_encoder"]!="sha-text_encoder"


def test_unequal_image_placement_shares_only_a_current_qualified_workflow(image_settings,monkeypatch):
    from types import SimpleNamespace
    import time
    from loadcoach.config import Config
    from loadcoach.models import Models
    from loadcoach.runner import Runner,Server
    settings={**image_settings,'alone':False,'sdcpp':{**image_settings['sdcpp'],'layer_weights':[3,1]}}
    library=Models(':memory:');library.put('flux',settings)
    runner=Runner(Config(gpus=('CUDA0','CUDA1'),card_margin=256),None,models=library)
    runner.builds[('sdcpp','sd-server')]=(None,'sdcpp:current')
    monkeypatch.setattr(runner,'cpu_room',lambda *a:[])
    resident=Server('text',('CUDA1',),{'flags':{'model':'text.gguf','ctx-size':512}},ready=True,mib=[3000],process=SimpleNamespace(pid=123))
    runner.servers['text']=resident;runner.holds[('text','owner')]=time.monotonic()+60
    runner.seen={'cards':{0:{'total':16000,'used':0},1:{'total':16000,'used':3000}},'pids':{123:{1:3000}}}
    library.measured('flux',settings,[12000,7000])
    assert runner.place('flux',settings,set()) is None
    library.measured('flux',settings,[12000,7000],image_profile={'qualified':True,'build':'sdcpp:old'})
    assert runner.place('flux',settings,set()) is None
    library.measured('flux',settings,[12000,7000],image_profile={'qualified':True,'build':'sdcpp:current'})
    assert runner.place('flux',settings,set())==(('CUDA0','CUDA1'),[])
    library.measured('flux',settings,None,image_profile={'qualified':False,'build':'sdcpp:current'})
    assert runner.fit('flux',settings,set()) is None  # the held text model keeps its card
    runner.holds.clear();runner.servers.clear();runner.seen['pids']={};runner.seen['cards'][1]['used']=0
    assert runner.fit('flux',settings,set())==(settings,('CUDA0','CUDA1'),[])
    reversed_cards={**settings,'gpus':['CUDA1','CUDA0']}
    assert runner.place('flux',reversed_cards,set())==(('CUDA1','CUDA0'),[])
    assert model.fixed_split(settings,2)
    settings['sdcpp'].pop('layer_weights')
    assert not model.fixed_split(settings,2)


def test_optional_install_configuration_is_scoped_private_and_idempotent(tmp_path):
    path=tmp_path/"loadcoach.toml"
    original='host = "127.0.0.1"\n[engines.llama_cpp]\nexecutable = "/keep/llama-server"\n[places]\nmodels = "/keep/models"\n'
    path.write_text(original)
    installer.configure(path,Path('/new/sd-server'),Path('/new/sd-cli'),Path('/new/installation.json'))
    parsed=tomllib.loads(path.read_text())
    assert parsed["engines"]["llama_cpp"]["executable"]=="/keep/llama-server"
    assert parsed["places"]=={"models":"/keep/models"}
    assert parsed["engines"]["sdcpp"]["enabled"] is True
    snapshots=list((tmp_path/'sdcpp-backups').glob('*.before-sdcpp-*'))
    assert len(snapshots)==1 and snapshots[0].read_text()==original
    assert path.stat().st_mode & 0o777==0o600
    installer.configure(path,Path('/new/sd-server'),Path('/new/sd-cli'),Path('/new/installation.json'))
    assert len(list((tmp_path/'sdcpp-backups').glob('*.before-sdcpp-*')))==1
    checked_installations({"sdcpp":parsed["engines"]["sdcpp"]},"llama-server")


def test_optional_engine_is_not_masked_by_older_web_changes_to_another_engine():
    from loadcoach.settings import laid_over
    file={'engines':{'sdcpp':{'enabled':True,'executable':'/new/sd-server'},'strata':{'enabled':False}}}
    changed={'engines':{'strata':{'enabled':True}}}
    result=laid_over(file,changed,'loadcoach')
    assert result['engines']['sdcpp']==file['engines']['sdcpp']
    assert result['engines']['strata']==changed['engines']['strata']
    changed['engines']['sdcpp']={'enabled':False}
    assert not laid_over(file,changed,'loadcoach')['engines']['sdcpp']['enabled']
    voice={'profiles':{'default':{'model':'a'},'soundroom':{'model':'a','speech':'audiocpp'}}}
    edited=laid_over(voice,{'profiles':{'default':{'model':'b'}}},'voice')['profiles']
    assert edited=={'default':{'model':'b'},'soundroom':{'model':'a','speech':'audiocpp'}}


def test_dry_run_does_not_create_installation_or_change_settings(tmp_path):
    result=subprocess.run([sys.executable,str(ROOT/"scripts/install-sdcpp.py"),"--dry-run","--root",str(tmp_path),"--prefix",str(tmp_path/"native")],text=True,capture_output=True)
    assert result.returncode==0 and "Would verify/build" in result.stdout
    assert list(tmp_path.iterdir())==[]


def test_corrupt_release_is_not_adopted_or_executed(tmp_path):
    (tmp_path/"bin").mkdir()
    binary=tmp_path/"bin/sd-server";binary.write_bytes(b"incorrect");binary.chmod(0o700)
    (tmp_path/"installation.json").write_text(json.dumps({"executables":{"sd-server":"wrong checksum"}}))
    with pytest.raises(ValueError,match="verification failed"): installer.verify(tmp_path)


class FakeNative:
    """Return deterministic image events without native processes or accelerators."""
    def __init__(self,fail=False):
        self.payloads=[];self.stops=0;self.fail=fail

    async def stop(self):
        self.stops+=1

    async def generate(self,payload):
        from PIL import Image
        import base64
        self.payloads.append(payload)
        if self.fail:raise RuntimeError("Fixture generation failure")
        encoded=base64.b64encode(png_bytes(Image.new('RGB',(payload['width'],payload['height']),'red'))).decode()
        yield {'type':'image_generation.partial_image','step':1,'total_steps':payload['sample_params']['sample_steps'],'b64_json':encoded}
        yield {'type':'native.completed','data':{'b64_json':encoded},'native_seconds':1.2}


def private_job(settings):
    return {'settings':settings,'token':'test-private-credential','installation':{'executable':'/native/sd-server','cli':'/native/sd-cli'},'cards':['CUDA0','CUDA1']}


def test_native_layout_stages_components_and_names_only_admitted_devices(image_settings):
    command=native_command(private_job(image_settings),12345)
    assert command[command.index('--backend')+1]=='diffusion=cuda0&cuda1,te=cuda0,vae=cuda0'
    assert command[command.index('--split-mode')+1]=='layer'
    assert command[command.index('--params-backend')+1]=='diffusion=disk,te=disk,vae=disk'


def test_cpu_image_placement_is_explicit_and_cannot_keep_a_gpu_split(image_settings):
    from copy import deepcopy
    cpu = deepcopy(image_settings)
    cpu['gpus'] = 0
    with pytest.raises(ValueError, match='single-device'):
        model.check_model('cpu-image',cpu,())
    cpu['sdcpp']['split_mode'] = 'single'
    model.check_model('cpu-image',cpu,())
    assert model.required_devices(cpu,()) == 0
    command = native_command(private_job(cpu),12345)
    assert command[command.index('--backend')+1] == 'diffusion=cpu,te=cpu,vae=cpu'
    assert command[command.index('--flash-attn')+1] == 'off'


def test_private_image_runtime_streams_previews_and_resolved_seeds(image_settings):
    native=FakeNative();job=private_job(image_settings)
    with TestClient(create_app(job,native)) as client:
        assert client.get('/health').status_code==401
        headers={'authorization':'Bearer '+job['token']}
        response=client.post('/v1/images/generations',headers=headers,json={
            'model':'flux2-dev','prompt':'A red boat','size':'256x256','seed':17,'n':2,'stream':True,'partial_images':1})
        assert response.status_code==200
        assert response.text.count('event: image_generation.partial_image')==2
        assert response.text.count('event: image_generation.completed')==2
        assert 'event: error' not in response.text
        assert [p['seed'] for p in native.payloads]==[17,18]
        assert native.payloads[0]['sample_params']['sample_steps']==28
        assert native.payloads[0]['embed_image_metadata'] is False
        assert client.post('/v1/images/generations',headers=headers,json={'model':'flux2-dev','prompt':'Again','size':'256x256'}).status_code==200


@pytest.mark.parametrize('family,cfg,scheduler,alignment',[
    ('qwen-image',4,'discrete',16),('qwen-image-2.1',6,'flux',32)])
def test_qwen_generation_keeps_native_sampling_negative_prompt_and_family_geometry(image_settings,family,cfg,scheduler,alignment):
    from copy import deepcopy
    settings=deepcopy(image_settings)
    settings['sdcpp']['family']=family
    model.check_model('qwen',settings,('CUDA0','CUDA1'))
    native=FakeNative();job=private_job(settings)
    command=native_command(job,12345)
    assert command[command.index('--diffusion-model')+1]==settings['flags']['model']
    assert command[command.index('--llm')+1]==settings['sdcpp']['components']['text_encoder']
    with TestClient(create_app(job,native)) as client:
        headers={'authorization':'Bearer '+job['token']}
        response=client.post('/v1/images/generations',headers=headers,json={
            'model':'qwen','prompt':'A sign reading OWG','negative_prompt':'blur','width':256,'height':256,'seed':42})
        assert response.status_code==200,response.text
        assert native.payloads[0]['negative_prompt']=='blur'
        assert native.payloads[0]['sample_params']['scheduler']==scheduler
        assert native.payloads[0]['sample_params']['guidance']['txt_cfg']==cfg
        assert native.payloads[0]['sample_params']['sample_steps']==50
        assert 'ref_images' not in native.payloads[0]
    with pytest.raises(ValueError,match='reference'):
        checked_request({'model':'qwen','prompt':'Edit','references':['eA==']},settings)
    if alignment==32:
        with pytest.raises(ValueError,match='multiples of 32'):
            checked_request({'model':'qwen','prompt':'Image','width':272,'height':256},settings)


def test_generation_failure_stops_native_before_releasing_private_slot(image_settings):
    native=FakeNative(fail=True);job=private_job(image_settings)
    with TestClient(create_app(job,native)) as client:
        answer=client.post('/v1/images/generations',headers={'authorization':'Bearer '+job['token']},json={'model':'flux2-dev','prompt':'A boat','size':'256x256'})
        assert answer.status_code==502
        assert native.stops>=1
        assert 'Fixture generation failure' in answer.json()['error']['message']
        streamed=client.post('/v1/images/generations',headers={'authorization':'Bearer '+job['token']},json={'model':'flux2-dev','prompt':'A boat','size':'256x256','stream':True})
        assert '"type": "error"' in streamed.text and 'Fixture generation failure' in streamed.text


def test_required_reference_upscaler_failure_never_becomes_plain_resize(image_settings):
    from PIL import Image
    import base64
    native=FakeNative();job=private_job(image_settings)
    small=base64.b64encode(png_bytes(Image.new('RGB',(32,32),'blue'))).decode()
    with TestClient(create_app(job,native)) as client:
        answer=client.post('/v1/images/edits',headers={'authorization':'Bearer '+job['token']},json={'model':'flux2-dev','prompt':'A boat','references':[small]})
        assert answer.status_code==502 and 'requires a configured model upscaler' in answer.json()['error']['message']
        assert native.payloads==[] and native.stops>=1


def test_reference_preparation_reuse_is_private_and_no_log_does_not_cache(image_settings,monkeypatch):
    import base64
    from PIL import Image
    from mirrorwall import runtime
    calls=[]
    async def upscale(job,image,target,folder,label):
        calls.append(target)
        return image.resize(target)
    monkeypatch.setattr(runtime,'upscale',upscale)
    image_settings['sdcpp']['upscaler']=image_settings['flags']['model']
    native=FakeNative();job=private_job(image_settings)
    small=base64.b64encode(png_bytes(Image.new('RGB',(32,32),'blue'))).decode()
    with TestClient(create_app(job,native)) as client:
        base={'authorization':'Bearer '+job['token']}
        asked={'model':'flux2-dev','prompt':'A boat','size':'256x256','references':[small]}
        for owner in ('one','one','two',None,None):
            headers={**base,**({'x-loadcoach-image-owner':owner} if owner else {})}
            assert client.post('/v1/images/edits',headers=headers,json=asked).status_code==200
    assert len(calls)==4
    assert len({p['ref_images'][0] for p in native.payloads})==1


def test_failed_observation_revokes_a_previous_shared_profile(image_settings,monkeypatch):
    from mirrorwall import runtime
    measurements=iter((True,False))
    class Meter:
        def __init__(self,cards):
            import asyncio
            self.stopping=asyncio.Event()
        async def run(self):
            await self.stopping.wait()
        def result(self):
            return {'complete':next(measurements),'gpu_peak_mib':[1,1],'rss_peak_mib':1}
    monkeypatch.setattr(runtime,'Meter',Meter)
    monkeypatch.setattr(runtime,'envelope',lambda *args:True)
    native=FakeNative();job=private_job(image_settings)
    with TestClient(create_app(job,native)) as client:
        headers={'authorization':'Bearer '+job['token']}
        asked={'model':'flux2-dev','prompt':'A boat','size':'256x256'}
        assert client.post('/v1/images/generations',headers=headers,json=asked).status_code==200
        assert client.get('/health',headers=headers).json()['image_profile']['qualified']
        assert client.post('/v1/images/generations',headers=headers,json=asked).status_code==200
        assert not client.get('/health',headers=headers).json()['image_profile']['qualified']


def test_mask_composite_preserves_unselected_pixels(image_settings):
    import base64
    import io
    from PIL import Image
    native=FakeNative();job=private_job(image_settings)
    source=base64.b64encode(png_bytes(Image.new('RGB',(1008,1008),'blue'))).decode()
    mask=Image.new('L',(1008,1008),0);mask.paste(255,(504,0,1008,1008))
    encoded=base64.b64encode(png_bytes(mask)).decode()
    with TestClient(create_app(job,native)) as client:
        answer=client.post('/v1/images/edits',headers={'authorization':'Bearer '+job['token']},json={
            'model':'flux2-dev','prompt':'A boat','size':'256x256','references':[source],'mask':encoded})
        assert answer.status_code==200
        image=Image.open(io.BytesIO(base64.b64decode(answer.json()['data'][0]['b64_json'])))
        assert image.getpixel((32,32))==(0,0,255)
        assert image.getpixel((224,32))==(255,0,0)
        before=len(native.payloads)
        refused=client.post('/v1/images/edits',headers={'authorization':'Bearer '+job['token']},json={'model':'flux2-dev','prompt':'A boat','size':'256x512','references':[source],'mask':encoded})
        assert refused.status_code==502 and 'aspect ratio' in refused.json()['error']['message']
        assert len(native.payloads)==before


def test_native_diagnostics_require_real_split_and_complete_lora_application(image_settings):
    import asyncio
    from types import SimpleNamespace
    from mirrorwall.runtime import Native
    native=Native(private_job(image_settings))
    async def read():
        output=asyncio.StreamReader()
        output.feed_data(b'diffusion row split requested; fallback to CUDA0\n')
        output.feed_data(b'(4 / 4) LoRA tensors have been applied, lora_file_path = /models/good.safetensors --- lora.hpp:1044\n')
        output.feed_data(b'(2 / 4) LoRA tensors have been applied, lora_file_path = /models/partial.safetensors\n')
        output.feed_data(b'(1 / 5) incompatible LoRA tensors have been skipped, lora_file_path = /models/partial.safetensors\n')
        output.feed_eof();native.process=SimpleNamespace(stdout=output)
        await native.read_output()
    asyncio.run(read())
    assert not native.row_split
    assert native.applied_loras=={'/models/good.safetensors'}
    assert '/models/partial.safetensors' in native.incompatible_loras


def test_failed_generation_revokes_a_previous_successful_envelope(image_settings,monkeypatch):
    from mirrorwall import runtime
    class Meter:
        def __init__(self,cards):
            import asyncio
            self.stopping=asyncio.Event()
        async def run(self):
            await self.stopping.wait()
        def result(self):
            return {'complete':True,'gpu_peak_mib':[1,1],'rss_peak_mib':1}
    monkeypatch.setattr(runtime,'Meter',Meter)
    monkeypatch.setattr(runtime,'envelope',lambda *args:True)
    native=FakeNative();job=private_job(image_settings)
    with TestClient(create_app(job,native)) as client:
        headers={'authorization':'Bearer '+job['token']};asked={'model':'flux2-dev','prompt':'A boat','size':'256x256'}
        assert client.post('/v1/images/generations',headers=headers,json=asked).status_code==200
        assert client.get('/health',headers=headers).json()['image_profile']['qualified']
        native.fail=True
        assert client.post('/v1/images/generations',headers=headers,json=asked).status_code==502
        assert not client.get('/health',headers=headers).json()['image_profile']['qualified']


def test_native_adapter_catalogue_contains_only_configured_files_and_is_removed(image_settings,tmp_path):
    import asyncio
    from mirrorwall.runtime import Native,native_error
    source=tmp_path/'adapter.safetensors';source.write_bytes(b'fixture adapter')
    (tmp_path/'unconfigured.safetensors').write_bytes(b'not selected')
    image_settings['sdcpp']['loras']={'style':{'file':str(source),'scale':1}}
    job=private_job(image_settings);job['scratch']=str(tmp_path)
    native=Native(job);native.prepare_loras();directory=Path(native.catalog.name)
    files=list(directory.iterdir());assert len(files)==1 and files[0].read_bytes()==source.read_bytes()
    assert native.lora_paths[str(source)]==files[0].name
    asyncio.run(native.stop());assert not directory.exists() and source.is_file()
    assert native_error({'error':'invalid lora path'})=='invalid lora path'
    assert native_error({'error':{'message':'out of memory'}})=='out of memory'


def test_smaller_success_after_reload_preserves_current_envelope_but_failure_revokes(image_settings,monkeypatch):
    import asyncio
    import httpx
    from loadcoach.config import Config
    from loadcoach.models import Models
    from loadcoach.runner import Runner,Server
    library=Models(':memory:');library.put('flux',image_settings)
    prior={'qualified':True,'build':'current','gpu_peak_mib':[12000,7000],'rss_peak_mib':2000}
    library.measured('flux',image_settings,[12256,7256],image_profile=prior)
    profile={'qualified':False,'successful':True,'complete':True,'gpu_peak_mib':[5000,3000],'rss_peak_mib':1000}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'image_profile':profile}))) as client:
            runner=Runner(Config(),client,models=library)
            monkeypatch.setattr(runner,'sample_resident',lambda server:None)
            server=Server('flux',('CUDA0','CUDA1'),image_settings,url='http://private',build='current')
            await runner.measure(server)
            assert library.measure('flux')['image_profile']['qualified']
            assert server.mib==[12256,7256]
            profile['successful']=False
            await runner.measure(server)
            assert library.measure('flux') is None and server.mib is None
    asyncio.run(run())


def test_extreme_reference_aspect_cannot_expand_the_pixel_envelope():
    from mirrorwall.runtime import reference_size
    with pytest.raises(ValueError,match='aspect ratio'):
        reference_size(32000000,1,.5)


def test_render_export_drops_inherited_reference_color_profile_and_exif():
    import io
    from PIL import Image
    source=Image.new('RGB',(16,16),'blue')
    exif=Image.Exif();exif[0x013B]='private reference identity'
    source.info.update(exif=exif.tobytes(),icc_profile=b'private profile')
    result=Image.composite(Image.new('RGB',(16,16),'red'),source,Image.new('L',(16,16),0))
    assert result.info
    with Image.open(io.BytesIO(png_bytes(result))) as exported:
        assert not exported.info and exported.getpixel((0,0))==(0,0,255)
