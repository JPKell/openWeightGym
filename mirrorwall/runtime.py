"""Private image workflow child. Runner owns admission, cgroup and all descendants.

There is no independent job queue: one admitted request owns the engine. Native
jobs are polled for previews and cancelled/cleaned before the response finishes.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, aclosing
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import time
import hashlib
import shutil
from collections import OrderedDict

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import httpx
import anyio
from PIL import Image,ImageOps
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse,StreamingResponse
from starlette.routing import Route

from mirrorwall.requests import checked_request,MAX_REQUEST_BYTES
from mirrorwall.telemetry import Meter,envelope

Image.MAX_IMAGE_PIXELS=33554432


def free_port():
    """Ask the OS for a private child listener port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0))
        return probe.getsockname()[1]


def png_bytes(image):
    """Encode clean RGB pixels without inherited prompt/EXIF metadata."""
    stream=io.BytesIO()
    image.save(stream,format="PNG",icc_profile=None,exif=b'',pnginfo=None)
    return stream.getvalue()


def decode_image(encoded):
    """Decode/normalize a bounded upload with explicit orientation and white alpha matte."""
    raw=base64.b64decode(encoded,validate=True)
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in ("PNG","JPEG","WEBP"):
            raise ValueError("References must be PNG, JPEG or WebP.")
        if source.width*source.height>Image.MAX_IMAGE_PIXELS:
            raise ValueError("Reference exceeds the 32-megapixel limit.")
        source.load()
        image=ImageOps.exif_transpose(source)
        if "A" in image.getbands():
            rgba=image.convert("RGBA")
            background=Image.new("RGBA",image.size,"white")
            background.alpha_composite(rgba)
            return background.convert("RGB")
        return image.convert("RGB")


def reference_size(width,height,megapixels):
    """Preserve aspect ratio while satisfying Flux's VAE/latent packing alignment."""
    if max(width,height)>8*min(width,height):
        raise ValueError('Reference aspect ratio must be between 1:8 and 8:1; crop the original before uploading.')
    factor=math.sqrt(megapixels*1_000_000/(width*height))
    return max(16,round(width*factor/16)*16),max(16,round(height*factor/16)*16)


def native_command(job,port):
    """Translate an admitted profile into fixed native arguments, not user CLI text."""
    settings=job["settings"]
    options=settings["sdcpp"]
    components=options.get("components",{})
    mode=options.get("split_mode","single")
    diffusion="cuda0&cuda1" if mode in ("layer","row") else "cuda0"
    cpu = settings.get('gpus') == 0
    backend = 'diffusion=cpu,te=cpu,vae=cpu' if cpu else f"diffusion={diffusion},te={'cuda1' if mode=='components' else 'cuda0'},vae=cuda0"
    budget=",".join(f"cuda{i}={v}" for i,v in enumerate(options["vram_limit_gib"])) if options.get("vram_limit_gib") else "-1.5"
    command=[job["installation"]["executable"],"--listen-ip","127.0.0.1","--listen-port",str(port),
             # an SDXL checkpoint carries its own CLIP and VAE and loads as a full model; Flux 2 and SDXL UNet weights load alone
             "--model" if options["family"]=="sdxl" and not (components.get("clip_l") or components.get("clip_g")) else "--diffusion-model",
             settings["flags"]["model"],"--backend",backend,
             "--auto-fit","off","--max-vram",budget,"--conditioning-cache-size","0",
             "--rng","cpu","--threads",str(options.get("threads",4)),"--flash-attn","off" if cpu else "on"]
    if mode in ("layer","row"):
        command += ["--split-mode",mode]
    if options.get("staged",True):
        command += ["--params-backend","diffusion=disk,te=disk,vae=disk"]
    for key,flag in (("text_encoder","--llm"),("vae","--vae"),("clip_l","--clip_l"),("clip_g","--clip_g"),("tokenizer","--tokenizer")):
        if components.get(key):
            if key=='text_encoder' and options.get('family','').startswith('wan'):
                flag='--t5xxl'
            command += [flag,components[key]]
    for key,flag in (('high_noise_diffusion_model','--high-noise-diffusion-model'),('audio_vae','--audio-vae'),
                     ('embeddings_connectors','--embeddings-connectors'),('byt5','--t5xxl'),('clip_vision','--clip_vision'),
                     ('tae','--taesd')):
        if components.get(key):
            command += [flag,components[key]]
    if components.get('latent_upscaler'):
        command += ['--hires-upscalers-dir',str(Path(components['latent_upscaler']).parent)]
    return command


async def native_lines(stream):
    """Read native newline logs and carriage-return progress without waiting for job completion."""
    pending=b''
    while chunk:=await stream.read(4096):
        pieces=re.split(b'[\r\n]',pending+chunk)
        pending=pieces.pop()
        if len(pending)>2**22:
            raise ValueError('Native diagnostic line exceeds its bound.')
        for piece in pieces:
            if piece:
                yield piece+b'\n'
    if pending:
        yield pending


class Native:
    """A subordinate native server whose full lifetime belongs to the admitted wrapper."""
    def __init__(self,job):
        """Initialize private image state without loading model weights."""
        self.job=job
        self.process=None
        self.reader=None
        self.client=None
        self.catalog=None
        self.model_catalog=None
        self.model_files={}
        self.lora_paths={}
        self.output=""
        self.split_devices=set()
        self.row_split=False
        self.applied_loras=set()
        self.incompatible_loras=set()
        self.capabilities={}
        self.stage='loading'
        self.step_progress={}

    async def read_output(self):
        """Drain native output privately and retain only bounded ephemeral diagnostics."""
        async for line in native_lines(self.process.stdout):
            text=line.decode(errors="replace")
            self.output=(self.output+text)[-262144:]
            if self.job.get("qualification_log"):
                with Path(self.job["qualification_log"]).open("a") as record:
                    record.write(text)
            if re.search(r"(?:flux|qwen|moss|krea2|diffusion|dit|unet|wan(?!_vae)|ltxav|hunyuan_video(?!_vae)|minimax_h3(?!_audio_vae|_vae)).*graph-cut layer split:.*segments",text,re.I):
                if match:=re.search(r"CUDA\s*([0-9]+)",text,re.I):
                    self.split_devices.add(int(match[1]))
            if re.search(r"(?:flux|qwen|moss|krea2|diffusion|dit|unet|wan(?!_vae)|ltxav|hunyuan_video(?!_vae)|minimax_h3(?!_audio_vae|_vae)).*row split: [1-9][0-9]* tensors .*split across 2 devices",text,re.I):
                self.row_split=True
            clean=re.sub(r'\x1b\[[0-9;]*m','',text).strip()
            clean=re.sub(r' --- [^\r\n]+:\d+$','',clean)
            if self.job['settings']['sdcpp'].get('mode')=='vid_gen':
                lower=clean.lower()
                if 'get_learned_condition' in lower or 'computing condition' in lower:
                    self.stage='encoding prompt'
                if 'generate_video ' in lower:
                    self.stage='sampling'
                if 'sampling(high noise) completed' in lower:
                    self.stage='sampling · low noise'
                elif 'sampling completed' in lower or 'generating latent video completed' in lower:
                    self.stage='decoding'
                if 'decode_first_stage completed' in lower or 'decode_video_outputs returned frames' in lower:
                    self.stage='encoding container'
                if (match:=re.search(r'(\d+)\s*/\s*(\d+)\s*[-|]',clean)) and re.search(r's/it|it/s',clean):
                    self.step_progress={'step':int(match[1]),'total_steps':int(match[2])}
            if match:=re.search(r'\((\d+) / (\d+)\) LoRA tensors have been applied, lora_file_path = (.+)',clean):
                if int(match[1])>0 and match[1]==match[2]:
                    self.applied_loras.add(match[3])
                else:
                    self.incompatible_loras.add(match[3])
            if match:=re.search(r'incompatible LoRA tensors have been skipped, lora_file_path = (.+)',clean):
                self.incompatible_loras.add(match[1])

    def failure_detail(self):
        """Expose bounded native failure diagnostics without echoing prompts."""
        errors=[line for line in self.output.splitlines() if re.search(r"error|failed|cannot",line,re.I) and "prompt" not in line.lower()]
        return "; ".join(dict.fromkeys(errors[:3]+errors[-2:]))[-2400:]

    def prepare_loras(self):
        """Expose exactly configured adapters through native's relative-path catalogue."""
        if not self.job['settings']['sdcpp'].get('loras'):
            return
        self.catalog=tempfile.TemporaryDirectory(prefix='loras-',dir=self.job.get('scratch'))
        for entry in self.job['settings']['sdcpp']['loras'].values():
            source=Path(entry['file']).resolve()
            name=hashlib.sha256(str(source).encode()).hexdigest()+source.suffix
            target=Path(self.catalog.name)/name
            if not target.exists():
                try:
                    os.link(source,target)
                except OSError:
                    if shutil.disk_usage(self.catalog.name).free<source.stat().st_size+2**30:
                        raise ValueError('Insufficient scratch disk to stage an adapter from another filesystem.')
                    shutil.copyfile(source,target)
                    target.chmod(0o600)
                    with source.open('rb') as a,target.open('rb') as b:
                        if hashlib.file_digest(a,'sha256').digest()!=hashlib.file_digest(b,'sha256').digest():
                            raise ValueError('Adapter changed while staging its private native catalogue.')
            self.lora_paths[entry['file']]=name

    async def start(self):
        """Load native components lazily after reference preparation has released its VRAM."""
        if self.process and self.process.returncode is None:
            return
        port=free_port()
        self.output=""
        self.split_devices.clear()
        self.row_split=False
        self.applied_loras.clear()
        self.incompatible_loras.clear()
        self.prepare_loras()
        environment={key:value for key,value in os.environ.items() if key!='SD_SUITE_SPLIT_WEIGHTS'}
        if self.job['settings']['sdcpp'].get('layer_weights'):
            environment['SD_SUITE_SPLIT_WEIGHTS']=','.join(str(v) for v in self.job['settings']['sdcpp']['layer_weights'])
        command=native_command(self.job,port)
        video=self.job['settings']['sdcpp'].get('mode')=='vid_gen'
        if video:
            self.prepare_video_catalog()
            # CLI preloading advertises an all-features recipe. Load the precise upstream
            # recipe instead so input availability comes from the actual loaded context.
            flags={'--model','--diffusion-model','--llm','--t5xxl','--vae','--clip_l','--clip_g','--tokenizer',
                   '--high-noise-diffusion-model','--audio-vae','--embeddings-connectors','--clip_vision','--taesd'}
            command=[value for index,value in enumerate(command) if value not in flags and (index==0 or command[index-1] not in flags)]
            command+=['--model-dir',self.model_catalog.name]
        if self.catalog:
            command+=['--lora-model-dir',self.catalog.name]
        self.process=await asyncio.create_subprocess_exec(*command,env=environment,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,limit=2**22)
        self.reader=asyncio.create_task(self.read_output())
        self.client=httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",timeout=30)
        began=time.monotonic()
        while time.monotonic()-began<self.job["settings"]["sdcpp"].get("timeout",1800):
            if self.process.returncode is not None:
                raise RuntimeError("sdcpp failed to load: "+(self.failure_detail() or "check component compatibility and memory limits"))
            try:
                answer=await self.client.get("/v1/models")
                if answer.status_code==200 and video:
                    await self.load_video_recipe()
                    return
                if answer.status_code==200 and answer.json().get("data"):
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
        raise TimeoutError("sdcpp component loading timed out.")

    def prepare_video_catalog(self):
        """Expose only configured components in native category directories, never caller paths."""
        self.model_catalog=tempfile.TemporaryDirectory(prefix='video-models-',dir=self.job.get('scratch'))
        options=self.job['settings']['sdcpp']
        components={'diffusion_model':self.job['settings']['flags']['model'],**options.get('components',{})}
        slots={'text_encoder':'t5xxl' if options['family'].startswith('wan') else 'llm','byt5':'t5xxl'}
        self.model_files={}
        for key,path in components.items():
            if key=='latent_upscaler':
                continue
            slot=slots.get(key,key)
            directory='diffusion_models' if slot in ('diffusion_model','high_noise_diffusion_model') else 'vae' if slot in ('vae','audio_vae','tae') else 'clip_vision' if slot=='clip_vision' else 'text_encoders'
            source=Path(path).resolve()
            relative=directory+'/'+hashlib.sha256(str(source).encode()).hexdigest()+source.suffix
            target=Path(self.model_catalog.name)/relative
            target.parent.mkdir(exist_ok=True)
            try:
                os.link(source,target)
            except OSError:
                if shutil.disk_usage(target.parent).free<source.stat().st_size+2**30:
                    raise ValueError('Insufficient scratch disk to stage video components across filesystems.')
                shutil.copyfile(source,target)
                target.chmod(0o600)
                with source.open('rb') as a,target.open('rb') as b:
                    if hashlib.file_digest(a,'sha256').digest()!=hashlib.file_digest(b,'sha256').digest():
                        raise ValueError('Video component changed while staging.')
            self.model_files[slot]=relative

    async def load_video_recipe(self):
        """Resolve opaque native file IDs and load the pinned server's component-aware recipe."""
        from mirrorwall.video import recipe
        response=await self.client.get('/sdcpp/v1/model-files')
        response.raise_for_status()
        files={one['relative_path']:one['id'] for one in response.json()['files']}
        models={slot:files[path] for slot,path in self.model_files.items()}
        selected=recipe(self.job['settings'])
        # WAN VACE-Fun pairs extend the upstream VACE recipe with its native
        # high-noise slot; native resolves that slot's feature declaration itself.
        if selected=='wan21-vace' and 'high_noise_diffusion_model' in models:
            recipes=await self.client.get('/sdcpp/v1/recipes');recipes.raise_for_status()
            base=next(one for one in recipes.json()['recipes'] if one['id']==selected)
            definition={key:value for key,value in base.items() if key in ('schema_version','name','description','model_url','required_any','defaults','context','features','disabled_features')}
            definition['id']='owg-video-profile'
            definition['slots']=[{key:value for key,value in slot.items() if key in ('id','label','required')} for slot in base['slots']]+[{'id':'high_noise_diffusion_model','required':True}]
            directory=Path(self.model_catalog.name)/'recipes';directory.mkdir()
            (directory/'profile.json').write_text(json.dumps(definition))
            refreshed=await self.client.post('/sdcpp/v1/models/refresh');refreshed.raise_for_status()
            selected=definition['id']
        response=await self.client.post('/sdcpp/v1/models/load',json={'recipe':selected,'models':models},
                                        timeout=self.job['settings']['sdcpp'].get('limits',{}).get('max_job_seconds',1800))
        if response.status_code>=400:
            raise ValueError('Native video recipe could not load: '+native_error(response.json()))

    async def stop(self):
        """Stop and await native exit before another GPU stage or reservation release."""
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(),15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            await self.reader
        if self.client:
            await self.client.aclose()
        self.process=self.reader=self.client=None
        if self.catalog:
            self.catalog.cleanup()
            self.catalog=None
        self.lora_paths.clear()
        if self.model_catalog:
            self.model_catalog.cleanup()
            self.model_catalog=None
        self.model_files.clear()

    async def generate(self,payload):
        """Yield progress/previews and the final image; never return before cancellation settles."""
        await self.start()
        payload={**payload,'lora':[{**one,'path':self.lora_paths[one['path']]} for one in payload.get('lora',[])]}
        video=self.job['settings']['sdcpp'].get('mode')=='vid_gen'
        answer=await self.client.post("/sdcpp/v1/vid_gen" if video else "/sdcpp/v1/img_gen",json=payload)
        if answer.status_code>=400:
            raise RuntimeError("sdcpp rejected the generation settings: "+native_error(answer.json()))
        identifier=answer.json()["id"]
        done=False
        previous=None
        began=time.monotonic()
        try:
            while time.monotonic()-began<self.job["settings"]["sdcpp"].get('limits',{}).get('max_job_seconds',self.job["settings"]["sdcpp"].get("timeout",1800)):
                response=await self.client.get("/sdcpp/v1/jobs/"+identifier)
                response.raise_for_status()
                value=response.json()
                if video:
                    progress=value.get('progress') or {}
                    yield {'type':'video_generation.progress','stage':value.get('stage',self.stage),
                           'progress':progress or self.step_progress,'queue_position':value.get('queue_position',0)}
                preview=value.get("preview")
                if preview and preview.get("b64_json") and preview.get("step")!=previous:
                    previous=preview["step"]
                    yield {"type":"video_generation.partial_image" if video else "image_generation.partial_image",**preview}
                status=value["status"]
                if status=="completed":
                    mode=self.job["settings"]["sdcpp"].get("split_mode","single")
                    if mode=="layer" and not {0,1}<=self.split_devices:
                        diagnostics="; ".join(line for line in self.output.splitlines() if "split" in line.lower())[-2400:]
                        raise RuntimeError("Requested diffusion layer split was not confirmed on both GPUs; no single-card fallback accepted. "+diagnostics)
                    if mode=="row" and not self.row_split:
                        raise RuntimeError("Requested diffusion row split was not confirmed; no fallback accepted.")
                    expected=[str((Path(self.catalog.name)/one['path']).resolve()) for one in payload.get('lora',[])]
                    if any(path not in self.applied_loras or path in self.incompatible_loras for path in expected):
                        raise RuntimeError('The requested LoRA was not fully applied; check adapter/model compatibility.')
                    done=True
                    yield {"type":"native.completed","data":value["result"] if video else value["result"]["images"][0],
                           "native_seconds":(value["completed"]-value["started"])/1000,
                           "split_devices":sorted(self.split_devices)}
                    return
                if status in ("failed","cancelled"):
                    raise RuntimeError("sdcpp "+status+": "+native_error(value)+" "+self.failure_detail())
                await asyncio.sleep(.25)
            raise TimeoutError(('Video' if video else 'Image')+' generation exceeded the profile timeout; reduce the requested work or review the profile limit.')
        finally:
            if not done:
                # Ask native to cancel, then terminate and await exit if it cannot prove settlement.
                with anyio.CancelScope(shield=True):
                    try:
                        await asyncio.wait_for(self.client.post('/sdcpp/v1/jobs/'+identifier+'/cancel'),2)
                    except (httpx.HTTPError,TimeoutError,AttributeError):
                        pass
                # A failed HTTP client cannot prove cancellation; terminate the owned native child.
                await self.stop()


def native_error(value):
    """Normalize native string/object errors into a bounded clear API failure."""
    detail=value.get('error',value) if isinstance(value,dict) else value
    return str(detail.get('message','generation stopped') if isinstance(detail,dict) else detail)[:2000]


async def upscale(job,image,target,folder,label):
    """Run one native-factor model upscale, then Lanczos to the requested final size."""
    options=job["settings"]["sdcpp"]
    if not options.get("upscaler"):
        raise ValueError("This reference/output requires a configured model upscaler; ordinary enlargement is not a fallback.")
    source=folder/(label+"-input.png")
    output=folder/(label+"-upscaled.png")
    image.save(source)
    command=[job["installation"]["cli"],"-M","upscale","--init-img",str(source),"--output",str(output),
             "--upscale-model",options["upscaler"],"--upscale-repeats","1","--upscale-tile-size","512",
             "--backend","upscaler=cpu" if job['settings'].get('gpus') == 0 else "upscaler=cuda0","--threads",str(options.get("threads",4))]
    process=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(),options.get("timeout",1800))
        if process.returncode or not output.is_file():
            raise RuntimeError("Configured model upscaler failed; no ordinary-resize fallback was used.")
        with Image.open(output) as decoded:
            result=decoded.convert("RGB")
        return result.resize(target,Image.Resampling.LANCZOS) if result.size!=target else result
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(),10)
            except TimeoutError:
                process.kill()
                await process.wait()


class ReferenceCache:
    """Bound prepared PNG reuse to one authenticated owner and one resident profile."""
    def __init__(self):
        """Keep a bounded, expiring memory cache; never persist private references."""
        self.entries=OrderedDict()
        self.bytes=0

    def key(self,owner,encoded,target,upscaler):
        """Include original pixels, preparation version, dimensions and upscaler content."""
        if not owner:
            return None
        identity='none'
        if upscaler:
            with open(upscaler,'rb') as source:
                identity=hashlib.file_digest(source,'sha256').hexdigest()
        return (owner,hashlib.sha256(encoded.encode()).hexdigest(),'refprep-16-v1',target,identity)

    def get(self,key):
        """Return only unexpired derivatives within their owner's cache key."""
        now=time.monotonic()
        for old,(at,value) in list(self.entries.items()):
            if now-at>3600:
                self.bytes-=len(value)
                del self.entries[old]
        if key not in self.entries:
            return None
        self.entries.move_to_end(key)
        return self.entries[key][1]

    def put(self,key,value):
        """Evict least-recently-used entries before reaching 128 MiB or 64 references."""
        if key is None or len(value)>128*2**20:
            return
        if key in self.entries:
            self.bytes-=len(self.entries.pop(key)[1])
        while self.entries and (self.bytes+len(value)>128*2**20 or len(self.entries)>=64):
            _,(_,old)=self.entries.popitem(last=False)
            self.bytes-=len(old)
        self.entries[key]=(time.monotonic(),value)
        self.bytes+=len(value)


async def workflow(job,asked,native,cache=None,owner=None):
    """Prepare immutable inputs once, generate a bounded image batch, then upscale outputs."""
    settings=job["settings"]
    options=settings["sdcpp"]
    resolved=checked_request(asked,settings)
    start=time.monotonic()
    seed=resolved["seed"] if resolved["seed"]>=0 else secrets.randbelow(2**31-1)
    prepared=[]
    sources=[]
    generated=[]
    edit_mask=None
    with tempfile.TemporaryDirectory(prefix="job-",dir=job.get('scratch')) as temporary:
        folder=Path(temporary)
        for index,encoded in enumerate(resolved["references"]):
            yield {"type":"image_generation.progress","stage":"preparing","reference":index+1}
            image=await asyncio.to_thread(decode_image,encoded)
            sources.append(image)
            target=(resolved['width'],resolved['height']) if resolved.get('detail_resize') else reference_size(*image.size,resolved['reference_megapixels'])
            key=await asyncio.to_thread(cache.key,owner,encoded,target,options.get('upscaler')) if cache else None
            cached=cache.get(key) if cache and key else None
            if cached:
                prepared.append(cached)
                continue
            if image.width*image.height<target[0]*target[1]:
                await native.stop()
                image=await upscale(job,image,target,folder,"reference-"+str(index))
            elif image.size!=target:
                image=image.resize(target,Image.Resampling.LANCZOS)
            prepared.append(base64.b64encode(png_bytes(image)).decode())
            if cache:
                cache.put(key,prepared[-1])
        if 'mask' in resolved:
            with Image.open(io.BytesIO(base64.b64decode(resolved['mask']))) as uploaded:
                if uploaded.format!='PNG' or uploaded.width*uploaded.height>Image.MAX_IMAGE_PIXELS:
                    raise ValueError('Edit masks must be PNG images within the 32-megapixel limit.')
                uploaded=ImageOps.exif_transpose(uploaded)
                if uploaded.size!=sources[0].size:
                    raise ValueError('Edit mask dimensions must match the first reference image.')
                if sources[0].width*resolved['height']!=sources[0].height*resolved['width']:
                    raise ValueError('Masked output must preserve the first reference aspect ratio.')
                edit_mask=uploaded.convert('L')
                if 'A' in uploaded.getbands():
                    from PIL import ImageChops
                    edit_mask=ImageChops.lighter(edit_mask,ImageOps.invert(uploaded.getchannel('A')))
        for index in range(resolved["n"]):
            yield {"type":"image_generation.progress","stage":"loading","image_index":index}
            payload={"prompt":resolved["prompt"],"negative_prompt":resolved.get("negative_prompt",""),
                "width":resolved["width"],"height":resolved["height"],"seed":(seed+index)%(2**31),"batch_count":1,
                "embed_image_metadata":False,"output_format":"png","cache_mode":"disabled",
                "preview":"proj" if resolved.get("partial_images",0) or resolved.get('live_previews') else "none",
                "preview_interval":options.get("preview_interval",1),
                "sample_params":{"sample_steps":resolved["steps"],"sample_method":resolved["sampler"],"scheduler":resolved["scheduler"],
                    "guidance":{"txt_cfg":resolved["cfg_scale"],"distilled_guidance":resolved["guidance"]}}}
            if prepared:
                if options["family"]=="sdxl":
                    payload.update(init_image=prepared[0],strength=resolved["strength"])
                else:
                    payload.update(ref_images=prepared,image_preprocess="target=ref,mode=none",ref_image_args="resize_before_vae=false")
            payload["lora"]=[{"path":entry["file"],"multiplier":resolved.get("loras",{}).get(name,entry.get("scale",1))}
                              for name,entry in options.get("loras",{}).items() if resolved.get("loras",{}).get(name,entry.get("scale",1))!=0]
            yield {"type":"image_generation.progress","stage":"generating","image_index":index}
            preview_count=0
            async for event in native.generate(payload):
                if event["type"]=="native.completed":
                    generated.append((event["data"],event["native_seconds"]))
                elif event['type']=='image_generation.partial_image':
                    if resolved.get('live_previews') or preview_count<resolved.get('partial_images',0):
                        yield {**event,'partial_image_index':preview_count,'created_at':int(time.time()),'output_format':'png',
                               'background':'opaque','quality':'auto','size':f"{resolved['width']}x{resolved['height']}","image_index":index}
                        preview_count+=1
                else:
                    yield {**event,"image_index":index}
        resize_target=resolved.get('detail_resize')
        if resolved["upscale"]>1 or resize_target:
            await native.stop()
        for index,(data,native_seconds) in enumerate(generated):
            image=decode_image(data["b64_json"])
            if edit_mask is not None:
                mask=edit_mask.resize(image.size,Image.Resampling.LANCZOS)
                original=sources[0].resize(image.size,Image.Resampling.LANCZOS)
                image=Image.composite(image,original,mask)
            if resize_target:
                final=(resize_target['width'],resize_target['height'])
                if final[0]>image.width or final[1]>image.height:
                    yield {"type":"image_generation.progress","stage":"upscaling","image_index":index}
                    image=await upscale(job,image,final,folder,"detail-output-"+str(index))
                elif image.size!=final:
                    image=image.resize(final,Image.Resampling.LANCZOS)
            elif resolved["upscale"]>1:
                yield {"type":"image_generation.progress","stage":"upscaling","image_index":index}
                target=(round(image.width*resolved["upscale"]),round(image.height*resolved["upscale"]))
                image=await upscale(job,image,target,folder,"output-"+str(index))
            yield {"type":"image_generation.completed","image_index":index,"b64_json":base64.b64encode(png_bytes(image)).decode(),
                   'created_at':int(time.time()),'output_format':'png','background':'opaque','quality':'auto','size':f'{image.width}x{image.height}',
                   "seed":(seed+index)%(2**31),"width":image.width,"height":image.height,
                   "settings":{k:v for k,v in resolved.items() if k not in ("references","mask","metadata","user")},
                   "native_seconds":native_seconds,"elapsed_seconds":time.monotonic()-start,
                   "mask_mode":"composite" if "mask" in resolved else None}


async def video_workflow(job,asked,native,cache=None,owner=None):
    """Run one bounded video through the same native child and progress/cancellation lifecycle."""
    from mirrorwall.video import checked_request as check_video, native_payload
    started=time.monotonic()
    yield {'type':'video_generation.progress','stage':'loading'}
    await native.start()
    answer=await native.client.get('/sdcpp/v1/capabilities')
    answer.raise_for_status()
    capabilities=answer.json()
    native.capabilities=capabilities
    resolved=check_video(asked,job['settings'],capabilities=capabilities)
    seed=resolved['seed'] if resolved['seed']>=0 else secrets.randbelow(2**31)
    resolved['seed']=seed
    # Never forward raw uploaded bytes, EXIF, filesystem paths or native arguments.
    for key in ('init_image','end_image'):
        if key in resolved:
            resolved[key]=base64.b64encode(png_bytes(decode_image(resolved[key]))).decode()
    for key in ('reference_images','control_video'):
        if key in resolved:
            resolved[key]=[base64.b64encode(png_bytes(decode_image(value))).decode() for value in resolved[key]]
    yield {'type':'video_generation.progress','stage':'encoding prompt'}
    async with aclosing(native.generate(native_payload(resolved,job['settings']))) as native_events:
        async for event in native_events:
            if event['type']!='native.completed':
                yield event
                continue
            data=event['data']
            frames=data.get('frame_count')
            fps=data.get('fps')
            if data.get('mime_type')!='video/webm' or data.get('output_format')!='webm' or frames!=resolved['frames'] or fps!=resolved['fps']:
                raise ValueError('Native video format, frame count or frame rate differs from the admitted request.')
            raw=base64.b64decode(data['b64_json'],validate=True)
            if not raw.startswith(b'\x1aE\xdf\xa3') or len(raw)>512*2**20:
                raise ValueError('Native video is invalid or exceeds the output limit.')
            yield {'type':'video_generation.progress','stage':'saving'}
            private={'init_image','end_image','reference_images','control_video','metadata','stream'}
            scale=2 if resolved.get('hires') else 1
            yield {'type':'video_generation.completed',**data,'seed':seed,'width':resolved['width']*scale,'height':resolved['height']*scale,
                   'mode':'references' if resolved.get('reference_images') else 'control' if resolved.get('control_video') else 'ends' if resolved.get('end_image') else 'start' if resolved.get('init_image') else 'text',
                   'soundtrack_present':bool(job['settings']['sdcpp'].get('components',{}).get('audio_vae')),
                   'seconds':frames/fps,'settings':{key:value for key,value in resolved.items() if key not in private},
                   'native_seconds':event['native_seconds'],'elapsed_seconds':time.monotonic()-started,
                   'gpu_seconds':(time.monotonic()-started)*len(job.get('cards',[])),
                   'runtime_capabilities':capabilities}
            native.seconds_per_output_second=(time.monotonic()-started)/(frames/fps)


def create_app(job,native=None):
    """Expose only authenticated private health and image routes to the owning Runner."""
    native=native or Native(job)
    busy=False
    profile=None
    cache=ReferenceCache()

    @asynccontextmanager
    async def lifespan(app):
        """Stop native descendants when the owning workflow listener closes."""
        try:
            yield
        finally:
            await native.stop()

    def authorized(request):
        """Verify the private credential belonging to this admitted session."""
        return secrets.compare_digest(request.headers.get("authorization",""),"Bearer "+job["token"])

    async def health(request):
        """Report readiness and resource evidence only to the owning Runner."""
        return JSONResponse({"status":"ready",'image_profile':profile}) if authorized(request) else JSONResponse({"error":{"message":"Unauthorized"}},401)

    async def load(request):
        """Measure real native component readiness without generating an image."""
        nonlocal busy
        if not authorized(request):
            return JSONResponse({"error":{"message":"Unauthorized"}},401)
        if busy:
            return JSONResponse({"error":{"message":"The runtime is busy."}},409)
        busy=True
        try:
            await native.start()
            capabilities=await native.client.get('/sdcpp/v1/capabilities')
            capabilities.raise_for_status()
            return JSONResponse({"loaded":True,"context_available":False,"basis":"native component readiness",
                                 'runtime_capabilities':capabilities.json()})
        finally:
            busy=False

    async def images(request:Request):
        """Validate a bounded image request and enforce one admitted job."""
        nonlocal busy
        from mirrorwall.video import is_video, checked_request as check_video
        video=request.url.path.startswith('/v1/videos/')
        if not authorized(request):
            return JSONResponse({"error":{"message":"Unauthorized"}},401)
        if video!=is_video(job['settings']):
            return JSONResponse({'error':{'message':'Choose a model for the requested media kind.'}},400)
        check=check_video if video else checked_request
        if busy:
            return JSONResponse({"error":{"message":"This image runtime already has an admitted job."}},409)
        try:
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>MAX_REQUEST_BYTES:
                    return JSONResponse({"error":{"message":"Image request exceeds 60 MiB."}},413)
            asked=json.loads(body)
            check(asked,job["settings"])
        except (ValueError,TypeError,Image.DecompressionBombError) as problem:
            return JSONResponse({"error":{"message":str(problem)}},400)
        busy=True

        async def events():
            """Own generation, sampling and cleanup until the entire request ends."""
            nonlocal busy,profile
            completed=False
            meter=Meter(job.get('monitor_cards', job['cards']))
            sampling=asyncio.create_task(meter.run())
            qualifying=envelope(check(asked,job['settings']),job['settings']) and not (profile and profile.get('qualified'))
            sequence=(video_workflow if video else workflow)(job,asked,native,None if qualifying else cache,request.headers.get('x-loadcoach-image-owner'))
            try:
                async with asyncio.timeout(job['settings']['sdcpp'].get('limits',{}).get('max_job_seconds',1800) if video else None):
                    async for event in sequence:
                        if event.get('stage'):
                            meter.stage=event['stage']
                        yield event
                completed=True
            finally:
                with anyio.CancelScope(shield=True):
                    await sequence.aclose()
                    if not completed:
                        await native.stop()
                    meter.stopping.set()
                    await sampling
                    measured=meter.result()
                    qualified=completed and measured['complete'] and envelope(check(asked,job['settings']),job['settings'])
                    if profile and profile.get('qualified'):
                        measured['gpu_peak_mib']=[max(a,b) for a,b in zip(profile['gpu_peak_mib'],measured['gpu_peak_mib'])]
                        measured['rss_peak_mib']=max(profile['rss_peak_mib'],measured['rss_peak_mib'])
                    profile={**measured,'successful':completed,'qualified':completed and measured['complete'] and (qualified or bool(profile and profile.get('qualified'))),'at':time.time(),
                             **({'runtime_capabilities':getattr(native,'capabilities',{}),
                                 'seconds_per_output_second':getattr(native,'seconds_per_output_second',None)} if video and completed else {})}
                    busy=False

        if asked.get("stream"):
            async def stream():
                """Serialize compatible image events and always close the workflow."""
                sequence=events()
                try:
                    async for event in sequence:
                        if request.url.path.endswith('/edits'):
                            event={**event,'type':event['type'].replace('image_generation.','image_edit.')}
                        yield "event: "+event["type"]+"\ndata: "+json.dumps(event)+"\n\n"
                except Exception as problem:
                    await native.stop()
                    detail=('Video generation exceeded the profile timeout; reduce the clip length or review its limit.' if video and isinstance(problem,TimeoutError) else str(problem).strip() or getattr(native,'failure_detail',lambda:'')() or type(problem).__name__)
                    yield "event: error\ndata: "+json.dumps({"type":"error","error":{"message":detail}})+"\n\n"
                finally:
                    await sequence.aclose()
            return StreamingResponse(stream(),media_type="text/event-stream",headers={"cache-control":"no-store","x-accel-buffering":"no"})
        try:
            output=[]
            async for event in events():
                if event["type"] in ("image_generation.completed","video_generation.completed"):
                    output.append({k:v for k,v in event.items() if k not in ("type","image_index")})
            return JSONResponse({"created":int(time.time()),"data":output},headers={"cache-control":"no-store"})
        except Exception as problem:
            await native.stop()
            detail=('Video generation exceeded the profile timeout; reduce the clip length or review its limit.' if video and isinstance(problem,TimeoutError) else str(problem).strip() or getattr(native,'failure_detail',lambda:'')() or type(problem).__name__)
            return JSONResponse({"error":{"message":detail}},502)

    return Starlette(lifespan=lifespan,routes=[Route("/health",health),Route("/load",load,methods=["POST"]),Route("/v1/images/generations",images,methods=["POST"]),Route("/v1/images/edits",images,methods=["POST"]),Route('/v1/videos/generations',images,methods=['POST'])])


if __name__=="__main__":
    import uvicorn
    job=json.loads(Path(sys.argv[1]).read_text())
    uvicorn.run(create_app(job),host="127.0.0.1",port=job["port"],access_log=False,log_level="warning",timeout_graceful_shutdown=10)
