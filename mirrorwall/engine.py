"""Native image engine adapter; LoadCoach owns its entire supervised session."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
from functools import lru_cache

from engines.contracts import Installation,Launch
from mirrorwall import model
from mirrorwall.requests import checked_request
from loadcoach.engine_memory import delegated_parent
from loadcoach.devices import cuda_environment, identity


@lru_cache(maxsize=16)
def stamped_build(stamps):
    """Bind qualification to native programs and the workflow/resource implementation."""
    value=hashlib.sha256()
    for path,*_ in stamps:
        with open(path,'rb') as stream:
            value.update(hashlib.file_digest(stream,'sha256').digest())
    return 'sdcpp:'+value.hexdigest()


def build_identity(options):
    """Fingerprint both native programs and the workflow/resource implementation."""
    files=[Path(shutil.which(options[key]) or options[key]).resolve() for key in ('executable','cli')]
    files += [Path(__file__).with_name(name) for name in ('runtime.py','model.py','requests.py','telemetry.py')]
    return stamped_build(tuple((str(path),path.stat().st_size,path.stat().st_mtime_ns,path.stat().st_ino) for path in files))


def node_options(config):
    """Validate the optional installation without invoking an executable."""
    options=getattr(config,"engines",{}).get("sdcpp",{})
    if not isinstance(options,dict) or options.keys()-{"enabled","executable","cli","manifest"}:
        raise ValueError("engines.sdcpp accepts enabled, executable, cli and manifest.")
    if type(options.get("enabled",False)) is not bool:
        raise ValueError("engines.sdcpp.enabled must be true or false.")
    for key in ("executable","cli","manifest"):
        if key in options and (not isinstance(options[key],str) or not options[key] or "\0" in options[key]):
            raise ValueError("Invalid sdcpp installation path.")
    executable=options.get("executable","sd-server")
    return {"enabled":options.get("enabled",False),"executable":executable,
            "cli":options.get("cli",str(Path(executable).with_name("sd-cli"))),"manifest":options.get("manifest","")}


class SDCpp:
    """Optional image configuration, resource admission and native process boundary."""
    id,label,model="sdcpp","stable-diffusion.cpp",model

    def installation(self,config):
        """Describe the explicitly enabled native runtime."""
        options=node_options(config)
        return Installation(self.id,options["enabled"],options["executable"])

    def schema(self):
        """Return image-specific editable settings and request capabilities."""
        return {"version":1,"id":self.id,"label":self.label,"configuration_key":"sdcpp",
                "schema_version":1,"selectable":False,
                "node":[{"name":"enabled","type":"boolean","label":"Enabled","default":False},
                        {"name":"executable","type":"string","label":"sd-server executable","default":"sd-server"},
                        {"name":"cli","type":"string","label":"sd-cli executable","default":"sd-cli"},
                        {"name":"manifest","type":"string","label":"Installation manifest (optional for manual setup)","default":""}],
                "families":list(model.FAMILIES),"split_modes":list(model.SPLITS),
                "capabilities":self.capabilities({}),"defaults":model.defaults({}),"limits":model.limits({})}

    def capabilities(self,settings):
        """Keep image generation distinct from language vision/chat capabilities."""
        return {"version":1,"chat":False,"streaming":False,"embeddings":False,"tokenize":False,
                "raw_completion":False,"prompt_rendering":False,"images":False,"tools":False,
                "image_generation":True,"image_editing":True,"image_streaming":True,
                "adapters":True,"native_probes":False,"load_probes":True,"masks":True}

    def describe(self,config,settings=None):
        """Expose availability and the dedicated image configuration schema."""
        options=node_options(config)
        found=shutil.which(options["executable"])
        available=bool(found and os.access(found,os.X_OK))
        return {"id":self.id,"label":self.label,**options,"available":available and options["enabled"],
                "why":"" if available and options["enabled"] else "Optional sdcpp image support is not installed/enabled. See Installation → Image generation.",
                "settings":options,"schema":self.schema(),"capabilities":self.capabilities(settings or {})}

    def host_ram_budget(self,config,settings):
        """Reserve the complete workflow's explicit host RAM cap."""
        return settings.get("sdcpp",{}).get("host_ram_mib",8192)

    def check_available(self,config,settings):
        """Reject missing components or an unavailable delegated RAM ceiling before admission."""
        options=node_options(config)
        if not options["enabled"] or not shutil.which(options["executable"]):
            raise ValueError("sdcpp is not installed/enabled on this node; run scripts/install-sdcpp.sh.")
        if settings['sdcpp'].get('upscaler') and not shutil.which(options['cli']):
            raise ValueError('The configured upscaler requires sd-cli; run scripts/install-sdcpp.sh.')
        for label,path in model.files_of(settings).items():
            if not Path(path).is_file():
                raise ValueError("Missing image component: "+label)
        if settings['sdcpp'].get('layer_weights'):
            try:
                manifest=json.loads(Path(options['manifest']).read_text())
            except (OSError,ValueError):
                raise ValueError('Fixed layer weights require a verified openWeightGym native installation.') from None
            patch=Path(__file__).with_name('patches')/'balanced-layer-split.patch'
            if manifest.get('suite_patch_sha256')!=hashlib.sha256(patch.read_bytes()).hexdigest():
                raise ValueError('This native build does not support the current fixed layer weights; reinstall with scripts/install-sdcpp.sh.')
            for role,key in (('sd-server','executable'),('sd-cli','cli')):
                with open(options[key],'rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=manifest.get('executables',{}).get(role):
                        raise ValueError('Native executable differs from its verified installation.')
        delegated_parent()

    def launch(self,config,port,settings,cards):
        """Start a private orchestration child inside Runner's owned cgroup/session."""
        self.check_available(config,settings)
        options=node_options(config)
        expected = 0 if settings.get('gpus') == 0 else (1 if settings["sdcpp"].get("split_mode","single")=="single" else 2)
        if len(cards) != expected:
            raise ValueError("The admitted GPU count does not match the image profile.")
        directory=tempfile.TemporaryDirectory(prefix="suite-image-engine-")
        try:
            token=secrets.token_urlsafe(32)
            path=Path(directory.name)/"launch.json"
            path.write_text(json.dumps({"port":port,"token":token,"settings":settings,"installation":options,"cards":list(cards),
                                        'monitor_cards': [identity(config, card) if identity(config, card).startswith('GPU-') else card for card in cards],
                                        "scratch":directory.name}))
            path.chmod(0o600)
            around={**cuda_environment(config, cards),
                    "GGML_CUDA_ENABLE_UNIFIED_MEMORY":"0","OMP_NUM_THREADS":str(settings["sdcpp"].get("threads",4))}
            return Launch([sys.executable,"-I","-B",str(Path(__file__).with_name("runtime.py")),str(path)],around,
                          process_group=True,memory_mib=self.host_ram_budget(config,settings),
                          headers={"authorization":"Bearer "+token},cleanup=directory.cleanup,
                          build=build_identity(options),stop_seconds=20)
        except BaseException:
            directory.cleanup()
            raise

    async def version(self,executable):
        """Fingerprint the configured binary without loading CUDA or model weights."""
        path=shutil.which(executable)
        if not path:
            return ""
        def checksum():
            """Hash the native executable without starting its runtime."""
            with open(path,"rb") as stream:
                return "sdcpp:"+hashlib.file_digest(stream,"sha256").hexdigest()
        return await asyncio.to_thread(checksum)

    async def build_version(self,config):
        """Resolve complete runtime identity before shared placement and after restarts."""
        try:
            return await asyncio.to_thread(build_identity,node_options(config))
        except OSError:
            return ''

    async def supported_flags(self,executable,cache):
        """Image model configuration uses typed fields rather than arbitrary flags."""
        return []

    def check_request(self,path,asked):
        """Refuse chat/tokenization routes before an image model is loaded."""
        if path not in ("/v1/images/generations","/v1/images/edits"):
            raise ValueError("This model supports image generation/editing only.")
        checked_request(asked)

    def prepare_request(self,path,asked):
        """Normalize accepted image fields; profile-specific checks run in the child."""
        self.check_request(path,asked)
        return dict(asked)

    def healthy(self,response):
        """The private workflow child is ready without loading model weights yet."""
        return response.status_code==200

    def failure_reason(self,output):
        """Return a short error without leaking image prompts or native command paths."""
        return "Image runtime stopped; check component compatibility, the workflow limits and the GPU/RAM budget."

    def parse_form(self,submitted,read):
        """Parse the dedicated image form's typed fields and advanced JSON options."""
        raw=read("image_options","{}")
        try:
            options=json.loads(raw)
        except ValueError:
            raise ValueError("Image options must be a JSON object.") from None
        if not isinstance(options,dict):
            raise ValueError("Image options must be an object.")
        return {"engine":self.id,"flags":{"model":read("model",""),"ctx-size":0},
                "sdcpp":options,"gpus":int(read("image_gpus","1")),"alone":True}

    def preview(self,name,candidate,library,runner,supported,engine_error=""):
        """Return the shared fixed-profile setup review used by saved sets."""
        from loadcoach.media_setup import preview
        return preview(name, candidate, library, runner)

    def suggestions(self,settings,defaults,cards):
        """Image profiles have explicit workflow limits and no chat-template guesses."""
        return []
