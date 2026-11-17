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


def test_video_container_build_options_cannot_reuse_an_incompatible_release():
    flags=installer.build_flags('cuda','120')
    assert '-DSD_WEBM=ON' in flags and '-DSD_WEBP=ON' in flags
    release=lambda options:installer.release_identity(installer.REVISION,'cuda','patch',options)
    assert release(flags)!=release([one.replace('SD_WEBM=ON','SD_WEBM=OFF') for one in flags])
    assert release(flags)!=release(installer.build_flags('cuda','native'))


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_native_progress_is_delivered_before_a_newline():
    from mirrorwall.runtime import native_lines
    import asyncio
    stream=asyncio.StreamReader()
    stream.feed_data(b'loading\r1/20 - 6.4s/it\r')
    lines=native_lines(stream)
    assert await anext(lines)==b'loading\n'
    assert await anext(lines)==b'1/20 - 6.4s/it\n'
    stream.feed_eof()
    assert [line async for line in lines]==[]


@pytest.mark.anyio
async def test_video_loader_uses_native_recipe_capabilities_and_opaque_file_ids(tmp_path):
    from mirrorwall.runtime import Native
    from test_video_requests import profile
    import httpx
    settings=profile(recipe='wan21-t2v')
    for key in ('model',):
        path=tmp_path/'weights.gguf';path.write_bytes(b'weights');settings['flags'][key]=str(path)
    for name in ('vae','text_encoder'):
        path=tmp_path/(name+'.gguf');path.write_bytes(name.encode());settings['sdcpp']['components'][name]=str(path)
    native=Native({'settings':settings,'scratch':str(tmp_path)})
    native.prepare_video_catalog()
    seen=[]
    def respond(request):
        if request.url.path.endswith('/model-files'):
            return httpx.Response(200,json={'files':[{'relative_path':path,'id':'opaque:'+slot} for slot,path in native.model_files.items()]})
        body=json.loads(request.content);seen.append(body)
        return httpx.Response(200,json={'state':'ready','recipe':body['recipe']})
    native.client=httpx.AsyncClient(base_url='http://native',transport=httpx.MockTransport(respond))
    try:
        await native.load_video_recipe()
        assert seen==[{'recipe':'wan21-t2v','models':{'diffusion_model':'opaque:diffusion_model','vae':'opaque:vae','t5xxl':'opaque:t5xxl'}}]
    finally:
        await native.stop()
    assert not list(tmp_path.glob('video-models-*'))


@pytest.mark.parametrize('outcome',['complete','invalid','source-changed'])
def test_video_conversion_is_bounded_preserves_sources_and_publishes_only_verified_output(tmp_path,monkeypatch,outcome):
    import asyncio
    from test_mirrorwall_components import gguf,safetensors
    from loadcoach import host_memory
    folder=tmp_path/'image';folder.mkdir()
    source=Path(safetensors(folder/'hunyuan.safetensors',{
        'txt_in.individual_token_refiner.blocks.0.adaLN_modulation.1.weight':(8,8),'byt5_in.weight':(8,8)}))
    original=source.read_bytes()
    monkeypatch.setattr(host_memory,'memory_snapshot',lambda:{'available':131072,'service_available':131072})
    jobs=installer.VideoConversions(tmp_path/'conversions','/bin/true',lambda:{'cache':str(folder)})
    calls=[]
    async def command(identifier,*args):
        calls.append(args)
        target=Path(args[-1])
        if outcome=='invalid':target.write_bytes(b'GGUFbroken')
        else:gguf(target,{'weight':(8,8)})
        if outcome=='source-changed':source.write_bytes(original+b'changed')
    monkeypatch.setattr(jobs,'command',command)
    async def run():
        try:
            row=await jobs.convert('owner',str(source),ram_mib=2048,disk_mib=1024)
            await asyncio.gather(*tuple(jobs.jobs.tasks))
            return jobs.jobs.get(row['id'],'owner')
        finally:await jobs.close()
    row=asyncio.run(run())
    assert row['state']==('complete' if outcome=='complete' else 'failed')
    assert '--as='+str(2048*2**20) in calls[0] and '--fsize='+str(1024*2**20) in calls[0]
    output=source.with_name('hunyuan-Q8_0.gguf')
    assert output.exists()==(outcome=='complete')
    assert not list(folder.glob('.video-convert-*'))
    if outcome=='complete':
        assert source.read_bytes()==original
        receipt=json.loads(output.with_name(output.name+'.source.json').read_text())
        assert receipt['source_sha256']==installer.digest(source) and receipt['sha256']==installer.digest(output)


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
    assert '--lora-apply-mode' not in command


def test_split_diffusion_merges_loras_instead_of_applying_them_at_runtime(image_settings):
    from copy import deepcopy
    split=deepcopy(image_settings); split['sdcpp']['loras']={'style':{'file':'/models/style.safetensors','scale':1}}
    command=native_command(private_job(split),12345)
    assert command[command.index('--lora-apply-mode')+1]=='immediately'
    split['sdcpp']['split_mode']='single'
    assert '--lora-apply-mode' not in native_command(private_job(split),12345)


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
    ('qwen-image',4,'discrete',16),('qwen-image-2.1',6,'flux',32),('moss-image',3,'discrete',16),('krea2',3.5,'discrete',16)])
def test_text_image_generation_keeps_native_sampling_negative_prompt_and_family_geometry(image_settings,family,cfg,scheduler,alignment):
    from copy import deepcopy
    settings=deepcopy(image_settings)
    settings['sdcpp']['family']=family
    if family=='moss-image':
        with pytest.raises(ValueError,match='tokenizer'):
            model.check_model('moss',settings,('CUDA0','CUDA1'))
        settings['sdcpp']['components']['tokenizer']=str(Path(settings['flags']['model']).with_name('tokenizer.json'))
        Path(settings['sdcpp']['components']['tokenizer']).write_text('{}')
    model.check_model('qwen',settings,('CUDA0','CUDA1'))
    native=FakeNative();job=private_job(settings)
    command=native_command(job,12345)
    assert command[command.index('--diffusion-model')+1]==settings['flags']['model']
    assert command[command.index('--llm')+1]==settings['sdcpp']['components']['text_encoder']
    if family=='moss-image':
        assert command[command.index('--tokenizer')+1]==settings['sdcpp']['components']['tokenizer']
        assert model.files_of(settings)['tokenizer']==settings['sdcpp']['components']['tokenizer']
        assert engine_for('sdcpp').capabilities(settings)['image_editing'] is False
        assert engine_for('sdcpp').capabilities(settings)['masks'] is False
    with TestClient(create_app(job,native)) as client:
        headers={'authorization':'Bearer '+job['token']}
        response=client.post('/v1/images/generations',headers=headers,json={
            'model':'qwen','prompt':'A sign reading OWG','negative_prompt':'blur','width':256,'height':256,'seed':42})
        assert response.status_code==200,response.text
        assert native.payloads[0]['negative_prompt']=='blur'
        assert native.payloads[0]['sample_params']['scheduler']==scheduler
        assert native.payloads[0]['sample_params']['guidance']['txt_cfg']==cfg
        assert native.payloads[0]['sample_params']['sample_steps']==(20 if family=='krea2' else 50)
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


def test_segmented_lora_counts_add_up_and_restart_with_each_application(image_settings):
    import asyncio
    from types import SimpleNamespace
    from mirrorwall.runtime import Native
    native=Native(private_job(image_settings))
    async def read(*lines):
        output=asyncio.StreamReader()
        for line in lines: output.feed_data(line)
        output.feed_eof();native.process=SimpleNamespace(stdout=output)
        await native.read_output()
    asyncio.run(read(b'[I] apply lora immediately --- diffusion_engine.cpp:1672\n',
        *[b'[I] (%d / 6) LoRA tensors have been applied, lora_file_path = /c/a.safetensors --- lora.hpp:1044\n'%n for n in (0,4,2,0)]))
    assert native.applied_loras=={'/c/a.safetensors'}
    asyncio.run(read(b'[I] apply lora immediately\n',b'(0 / 6) LoRA tensors have been applied, lora_file_path = /c/a.safetensors\n',
        b'(5 / 6) LoRA tensors have been applied, lora_file_path = /c/a.safetensors\n'))
    assert native.applied_loras==set()
    asyncio.run(read(b'[I] apply lora at runtime\n',b'[W] Only (5 / 6) LoRA tensors have been applied, lora_file_path = /c/a.safetensors\n'))
    assert native.applied_loras==set()


def test_moss_multi_gpu_diagnostics_recognize_applied_layer_and_row_splits(image_settings):
    import asyncio
    from types import SimpleNamespace
    from mirrorwall.runtime import Native
    native=Native(private_job(image_settings))
    async def read():
        output=asyncio.StreamReader()
        output.feed_data(b'MOSS-Image graph-cut layer split: CUDA0, 2 segments\n')
        output.feed_data(b'MOSS-Image graph-cut layer split: CUDA1, 3 segments\n')
        output.feed_data(b'MOSS-Image row split: 42 tensors successfully split across 2 devices\n')
        output.feed_eof()
        native.process=SimpleNamespace(stdout=output)
        await native.read_output()
    asyncio.run(read())
    assert native.split_devices=={0,1} and native.row_split


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


def test_every_native_sampler_and_scheduler_is_accepted_and_earlier_names_map_to_native(image_settings):
    from mirrorwall import graph
    from mirrorwall.runtime import unsupported_by_runtime
    assert graph.NATIVE_SAMPLERS==model.SAMPLERS and len(model.SAMPLERS)==21 and len(model.SCHEDULERS)==16
    for sampler in model.SAMPLERS:
        assert checked_request({'model':'flux2-dev','prompt':'A boat','sampler':sampler},image_settings)['sampler']==sampler
    for scheduler in model.SCHEDULERS:
        checked_request({'model':'flux2-dev','prompt':'A boat','scheduler':scheduler},image_settings)
    # Earlier saved names reached the native server unchanged and silently became its default sampler.
    assert checked_request({'model':'flux2-dev','prompt':'A boat','sampler':'dpmpp2m'},image_settings)['sampler']=='dpm++2m'
    settings={**image_settings,'sdcpp':{**image_settings['sdcpp'],'defaults':{'sampler':'dpmpp2s_a'}}}
    assert model.defaults(settings)['sampler']=='dpm++2s_a'
    with pytest.raises(ValueError,match='sampler'):
        checked_request({'model':'flux2-dev','prompt':'A boat','sampler':'dpm++3m'},image_settings)
    capabilities={'samplers':['euler','dpm++2m'],'schedulers':['discrete'],'upscalers':[{'name':'Latent'}],'features':{'hires':True,'cache':False}}
    ask=lambda **extra:{'sample_params':{'sample_method':'euler','scheduler':'discrete'},**extra}
    assert unsupported_by_runtime(ask(),capabilities) is None
    assert unsupported_by_runtime({'sample_params':{'sample_method':'lms','scheduler':'discrete'}},capabilities)=='the sampler lms'
    assert unsupported_by_runtime(ask(hires={'upscaler':'Lanczos'}),capabilities)=='the hires upscaler Lanczos'
    assert unsupported_by_runtime(ask(cache_mode='easycache'),capabilities)=='step caching'
    assert unsupported_by_runtime(ask(cache_mode='disabled'),capabilities) is None
    assert unsupported_by_runtime(ask(),{}) is None  # older builds report nothing; the request validator still applies


@pytest.mark.parametrize('family,advanced,message',[
    ('flux2-dev',{'flow_shift':101},'Flow shift'),('sdxl',{'flow_shift':3},'flow-matching'),
    ('flux2-dev',{'clip_skip':2},'SDXL'),('sdxl',{'clip_skip':2.5},'integer'),('flux2-dev',{'shifted_timestep':500},'SDXL'),
    ('flux2-dev',{'eta':float('nan')},'Eta'),('flux2-dev',{'img_cfg':-1},'Image CFG'),
    ('flux2-dev',{'custom_sigmas':[1,0.5,0]},'steps \\+ 1'),('flux2-dev',{'custom_sigmas':[0.1]+[1]*28},'high to low'),
    ('flux2-dev',{'cache_mode':'ucache'},'does not apply'),('sdxl',{'cache_mode':'easycache'},'does not apply'),
    ('flux2-dev',{'cache_option':'threshold=1'},'cache_mode'),('flux2-dev',{'cache_mode':'easycache','cache_option':'x; rm'},'name=number'),
    ('sdxl',{'slg':{'scale':2}},'DiT'),('flux2-dev',{'slg':{'layer_start':.5,'layer_end':.2}},'before'),
    ('flux2-dev',{'slg':{'layers':list(range(17))}},'1–16'),('flux2-dev',{'hires':{'scale':4}},'size limits'),
    ('flux2-dev',{'hires':{'upscaler':'Model'}},'upscaler'),('flux2-dev',{'hires':{'denoise':.4}},'takes'),
    ('flux2-dev',{'backend':'cpu'},'Unsupported advanced'),('flux2-dev',[],'object'),
])
def test_advanced_sampling_is_allowlisted_bounded_and_family_aware(image_settings,family,advanced,message):
    settings={**image_settings,'sdcpp':{**image_settings['sdcpp'],'family':family}}
    with pytest.raises(ValueError,match=message):
        checked_request({'model':'m','prompt':'A boat','size':'1024x1024','advanced':advanced},settings)


def test_shared_residency_entries_choose_sampler_scheduler_and_cfg_within_their_measured_passes(image_settings):
    shared={**image_settings,'alone':False}
    chosen=checked_request({'model':'m','prompt':'A boat','sampler':'dpmpp2m','scheduler':'karras'},shared)
    assert chosen['sampler']=='dpm++2m' and chosen['scheduler']=='karras'
    # Measured with a negative-prompt pass (CFG other than 1): any CFG, including 1, which skips that pass.
    sdxl={**shared,'sdcpp':{**shared['sdcpp'],'family':'sdxl'}}
    assert model.defaults(sdxl)['cfg_scale']!=1
    for cfg in (1,3.5,9):
        assert checked_request({'model':'m','prompt':'A boat','cfg_scale':cfg},sdxl)['cfg_scale']==cfg
    # Measured at CFG 1: no room was measured for that pass, so the CFG stays 1.
    assert model.defaults(shared)['cfg_scale']==1
    with pytest.raises(ValueError,match='measured at CFG 1'):
        checked_request({'model':'m','prompt':'A boat','cfg_scale':2},shared)


def test_advanced_sampling_needs_an_exclusive_entry_and_excludes_other_upscaling(image_settings):
    shared={**image_settings,'alone':False}
    with pytest.raises(ValueError,match='exclusive'):
        checked_request({'model':'m','prompt':'A boat','advanced':{'eta':.5}},shared)
    checked_request({'model':'m','prompt':'A boat','advanced':{}},shared)
    with pytest.raises(ValueError,match='not both'):
        checked_request({'model':'m','prompt':'A boat','size':'512x512','upscale':2,'advanced':{'hires':{}}},
                        {**image_settings,'sdcpp':{**image_settings['sdcpp'],'upscaler':image_settings['sdcpp']['components']['vae']}})


def test_advanced_sampling_reaches_the_native_request(image_settings):
    native=FakeNative();job=private_job(image_settings)
    advanced={'eta':.4,'flow_shift':3.5,'img_cfg':2,'custom_sigmas':[1,.75,.5,.25,0],'vae_tiling':True,
              'cache_mode':'easycache','cache_option':'threshold=0.2','slg':{'scale':2.5,'layers':[7,8,9]},
              'hires':{'upscaler':'Latent (bicubic)','scale':1.5,'steps':6,'denoising_strength':.4}}
    with TestClient(create_app(job,native)) as client:
        headers={'authorization':'Bearer '+job['token']}
        response=client.post('/v1/images/generations',headers=headers,json={
            'model':'flux2-dev','prompt':'A red boat','size':'512x512','steps':4,'sampler':'dpmpp2m','scheduler':'karras','advanced':advanced})
        assert response.status_code==200,response.text
        payload=native.payloads[0]
        sample=payload['sample_params']
        assert sample['sample_method']=='dpm++2m' and sample['scheduler']=='karras' and sample['sample_steps']==4
        assert sample['eta']==.4 and sample['flow_shift']==3.5 and sample['custom_sigmas']==[1,.75,.5,.25,0]
        assert sample['guidance']['img_cfg']==2 and sample['guidance']['txt_cfg']==1
        assert sample['guidance']['slg']=={'scale':2.5,'layer_start':.01,'layer_end':.2,'layers':[7,8,9]}
        assert payload['vae_tiling_params']=={'enabled':True}
        assert payload['cache_mode']=='easycache' and payload['cache_option']=='threshold=0.2'
        assert payload['hires']=={'enabled':True,'upscaler':'Latent (bicubic)','target_width':768,'target_height':768,'steps':6,'denoising_strength':.4}
        assert 'clip_skip' not in payload and 'shifted_timestep' not in sample
        client.post('/v1/images/generations',headers=headers,json={'model':'flux2-dev','prompt':'Plain','size':'256x256'})
        assert native.payloads[1]['cache_mode']=='disabled' and 'hires' not in native.payloads[1]


def test_upscale_only_requests_take_one_image_and_a_named_installed_file(image_settings):
    import base64
    from PIL import Image
    small=base64.b64encode(png_bytes(Image.new('RGB',(32,32),'blue'))).decode()
    resolved=checked_request({'model':'m','image':small,'upscale_with':{'upscaler':'4x-UltraSharp.pth','factor':2}},image_settings)
    assert resolved['upscale_with']=={'upscaler':'4x-UltraSharp.pth','factor':2,'tile':512}
    for bad,why in (({'upscaler':'../etc/passwd'},'installed upscaler file'),({'upscaler':'/abs/x.pth'},'installed upscaler file'),
                    ({'upscaler':'a.pth','factor':5},'factor'),({'upscaler':'a.pth','tile':64},'tile'),({'upscaler':'a.pth','path':'x'},'takes')):
        with pytest.raises(ValueError,match=why):
            checked_request({'model':'m','image':small,'upscale_with':bad},image_settings)
    with pytest.raises(ValueError,match='one image'):
        checked_request({'model':'m','prompt':'x','upscale_with':{'upscaler':'a.pth'}},image_settings)
    with pytest.raises(ValueError,match='one image'):
        checked_request({'model':'m','image':[small,small],'upscale_with':{'upscaler':'a.pth'}},image_settings)


def test_upscale_only_runs_the_resolved_file_and_never_generates_or_qualifies(image_settings,monkeypatch):
    import base64
    from PIL import Image
    from mirrorwall import runtime
    calls=[]
    async def upscale(job,image,target,folder,label,model_path=None,tile=512):
        calls.append((target,model_path,tile))
        return image.resize(target)
    monkeypatch.setattr(runtime,'upscale',upscale)
    upscaler=Path(image_settings['flags']['model']).with_name('4x-UltraSharp.pth'); upscaler.write_bytes(b'esrgan')
    native=FakeNative();job=private_job(image_settings)
    small=base64.b64encode(png_bytes(Image.new('RGB',(40,24),'blue'))).decode()
    asked={'model':'flux2-dev','image':small,'upscale_with':{'upscaler':'4x-UltraSharp.pth','factor':2.5,'tile':256}}
    with TestClient(create_app(job,native)) as client:
        base={'authorization':'Bearer '+job['token']}
        missing=client.post('/v1/images/edits',headers=base,json=asked)
        assert missing.status_code==502 and 'not installed on this node' in missing.json()['error']['message']
        answer=client.post('/v1/images/edits',headers={**base,'x-loadcoach-upscaler-path':str(upscaler)},json=asked)
        assert answer.status_code==200,answer.text
        assert answer.json()['data'][0]['size']=='100x60'
        assert calls==[((100,60),str(upscaler),256)] and native.payloads==[]
