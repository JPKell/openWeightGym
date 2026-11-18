#!/usr/bin/env python3
"""Build or adopt an optional sdcpp runtime from upstream without restarting openWeightGym.

Without --revision, the newest commit on the upstream default branch is built.

Only the engine's TOML table and its private installation directory are changed.
Verified releases are immutable; existing processes keep their original binary.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadcoach.classifier_jobs import ClassifierJobs

REPOSITORY = "https://github.com/leejet/stable-diffusion.cpp.git"


def build_flags(backend, architectures="native"):
    """Include browser containers in every build and its immutable release identity."""
    flags=["-DCMAKE_BUILD_TYPE=Release","-DSD_WEBP=ON","-DSD_WEBM=ON","-DSD_SERVER_BUILD_FRONTEND=OFF","-DSD_BUILD_SHARED_LIBS=OFF","-DBUILD_SHARED_LIBS=OFF"]
    if backend=="cuda":
        flags += ["-DSD_CUDA=ON","-DCMAKE_CUDA_ARCHITECTURES="+architectures]
    elif backend=="vulkan":
        flags += ["-DSD_VULKAN=ON"]
    return flags


def release_identity(revision, backend, flags):
    """Avoid reusing a release compiled with different container or device options."""
    options=hashlib.sha256(json.dumps(flags,separators=(',',':')).encode()).hexdigest()[:12]
    return revision+"-"+backend+"-"+options


def latest_revision():
    """The commit upstream's default branch points at now."""
    answer=run(["git","ls-remote",REPOSITORY,"HEAD"],capture_output=True,text=True,timeout=60).stdout.split()
    if not answer or not re.fullmatch(r"[0-9a-f]{40}",answer[0]):
        raise ValueError("Could not read the newest stable-diffusion.cpp commit from "+REPOSITORY)
    return answer[0]


def digest(path):
    """Hash a finished executable before publishing it."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, **kwargs):
    """Run a fixed argument vector, never through a shell."""
    return subprocess.run([str(v) for v in command], check=True, **kwargs)


def configure(path, server, cli, manifest, backup_directory=None):
    """Replace only the engine table, preserving unrelated configuration bytes."""
    text = path.read_text() if path.exists() else ""
    options = {"enabled": True, "executable": str(server), "cli": str(cli), "manifest": str(manifest)}
    if tomllib.loads(text).get("engines",{}).get("sdcpp")==options:
        return
    block = "[engines.sdcpp]\n" + "".join(f"{k} = {str(v).lower() if type(v) is bool else json.dumps(v)}\n" for k,v in options.items())
    pattern = r"(?ms)^\[engines\.sdcpp\][ \t]*\n.*?(?=^\[|\Z)"
    made = re.sub(pattern, block + "\n", text) if re.search(pattern,text) else text.rstrip()+"\n\n"+block
    parsed = tomllib.loads(made)
    if parsed["engines"]["sdcpp"] != options:
        raise ValueError("Could not safely configure engines.sdcpp")
    if made == text:
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_directory=backup_directory or path.parent/"sdcpp-backups"
        backup_directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        backup=backup_directory/(path.name+".before-sdcpp-"+stamp)
        shutil.copy2(path,backup)
        backup.chmod(0o600)
    temporary=path.with_name(path.name+".sdcpp-tmp")
    with temporary.open("x") as stream:
        os.chmod(temporary,0o600)
        stream.write(made)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify(folder):
    """Verify installed hashes and required API flags without loading a model."""
    recorded=json.loads((folder/"installation.json").read_text())
    for name,checksum in recorded["executables"].items():
        path=folder/"bin"/name
        if not path.is_file() or not os.access(path,os.X_OK) or digest(path)!=checksum:
            raise ValueError("sdcpp executable verification failed: "+name)
    answer=run([folder/"bin"/"sd-server","--help"],capture_output=True,text=True,timeout=30)
    help_text=answer.stdout+answer.stderr
    for flag in ("--backend","--split-mode","--listen-ip","--diffusion-model"):
        if flag not in help_text:
            raise ValueError("sdcpp build does not expose required flag "+flag)
    return recorded


def configured(root):
    """The release folder settings/loadcoach.toml runs sdcpp from, or None when it runs none."""
    try:
        table=tomllib.loads((root/"settings/loadcoach.toml").read_text()).get("engines",{}).get("sdcpp") or {}
    except FileNotFoundError:
        return None
    return Path(table["manifest"]).expanduser().parent if table.get("enabled") and table.get("manifest") else None


class VideoConversions(ClassifierJobs):
    """Tracked inert-weight conversions using the existing private Files job/process lifecycle."""
    async def convert(self,owner,path,*,ram_mib=16384,disk_mib=65536):
        """Reserve bounded CPU conversion work without changing source files or model settings."""
        from mirrorwall.components import identify
        from loadcoach.host_memory import memory_snapshot
        source=Path(path).resolve()
        roots=[Path(folder).resolve() for folder in self.places().values() if folder and Path(folder).name in ('image','video')]
        if not any(source.is_relative_to(root) for root in roots) or source.suffix.lower()!='.safetensors' or not source.is_file():
            raise ValueError('Choose a safetensors file inside this node’s image/video component library.')
        detected=await __import__('asyncio').to_thread(identify,str(source),roots)
        if detected.get('family')!='hunyuan-video-1.5' and detected.get('variant')!='ltx-gemma4-12b':
            raise ValueError('Conversion supports HunyuanVideo 1.5 weights or the LTX-2.5 Gemma encoder with bundled projection.')
        if type(ram_mib) is not int or not 2048<=ram_mib<=131072 or type(disk_mib) is not int or not 1024<=disk_mib<=524288:
            raise ValueError('Use explicit bounded RAM and temporary disk budgets in MiB.')
        limits=memory_snapshot()
        available=min(limits['available'],limits['service_available'] if limits['service_available'] is not None else limits['available'])
        if available<ram_mib+1024:
            raise ValueError('Host/service RAM cannot cover this conversion budget plus 1 GiB headroom.')
        if shutil.disk_usage(source.parent).free<(disk_mib+1024)*2**20 or source.stat().st_size*2>disk_mib*2**20:
            raise ValueError('Temporary disk budget or free space is insufficient for conversion.')
        prlimit=shutil.which('prlimit')
        if not prlimit or not shutil.which(str(self.server)):
            raise ValueError('Install sd-cli through scripts/install-sdcpp.sh and util-linux prlimit on this node.')
        output=source.with_name(source.stem+'-Q8_0.gguf')
        if output.exists():
            raise ValueError('The Q8_0 output already exists; retain it or choose another source.')
        row=self.jobs.create(owner,'video-conversion',source.name)

        async def worker(identifier):
            """Run bounded native conversion, verify the GGUF header and publish provenance atomically."""
            import asyncio
            try:
                self.jobs.update(identifier,progress='Converting video component to Q8_0',ram_mib=ram_mib,disk_mib=disk_mib,output=str(output))
                source_hash=await asyncio.to_thread(digest,source)
                with tempfile.TemporaryDirectory(prefix='.video-convert-',dir=source.parent) as temporary:
                    target=Path(temporary)/'output.gguf'
                    await asyncio.wait_for(self.command(identifier,prlimit,'--as='+str(ram_mib*2**20),
                        '--fsize='+str(disk_mib*2**20),'--cpu=3600',self.server,'-M','convert','-m',source,
                        '--type','q8_0','-o',target),3700)
                    if not target.is_file() or target.stat().st_size>disk_mib*2**20:
                        raise ValueError('Native conversion did not produce a bounded GGUF output.')
                    from engines.llama_cpp.gguf import read_metadata
                    await asyncio.to_thread(read_metadata,str(target),shapes=True)
                    if source_hash!=await asyncio.to_thread(digest,source):
                        raise ValueError('Source weights changed during conversion; output was discarded.')
                    receipt={'operation':'sdcpp-convert','source_sha256':source_hash,
                             'sha256':await asyncio.to_thread(digest,target),'type':'q8_0',
                             'native_sha256':await asyncio.to_thread(digest,Path(shutil.which(str(self.server)) or self.server)),
                             'ram_mib':ram_mib,'disk_mib':disk_mib}
                    # Hard linking refuses an output installed concurrently; the source is untouched.
                    os.link(target,output)
                    sidecar=output.with_name(output.name+'.source.json')
                    try:
                        with sidecar.open('x') as stream:
                            os.chmod(sidecar,0o600);json.dump(receipt,stream);stream.flush();os.fsync(stream.fileno())
                    except BaseException:
                        output.unlink(missing_ok=True)
                        raise
                await asyncio.to_thread(self.changed,output)
                self.jobs.update(identifier,progress='Converted; inference qualification still required',provenance=receipt)
            except Exception as problem:
                self.jobs.update(identifier,state='failed',error=str(problem)[:500])
        return self.jobs.start(row,worker)


def main():
    """Install a verified release and register it for subsequent engine launches."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--prefix",type=Path,help="where releases are kept (default: ROOT/runtimes/sdcpp)")
    parser.add_argument("--backend",choices=("cuda","vulkan","cpu"),default="cuda")
    parser.add_argument("--revision",help="full commit SHA to build (default: the newest upstream commit)")
    parser.add_argument("--source",type=Path,help="Existing clean source checkout; otherwise fetch upstream")
    parser.add_argument("--adopt-build",type=Path,help="Copy the bin directory of an existing build of the selected revision")
    parser.add_argument("--cuda-architectures",default="native")
    parser.add_argument("--jobs",type=int,default=4)
    parser.add_argument("--check",action="store_true")
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    args.root=args.root.expanduser().resolve()
    chosen=args.prefix is not None
    args.prefix=(args.prefix or args.root/"runtimes/sdcpp").expanduser().resolve()
    if args.revision is not None and not re.fullmatch(r"[0-9a-f]{40}",args.revision) or not 1<=args.jobs<=32:
        parser.error("Use a full commit SHA and 1–32 build jobs")
    if not re.fullmatch(r"native|[0-9]+(?:;[0-9]+)*",args.cuda_architectures):
        parser.error("Invalid CUDA architectures")
    current=args.prefix/"current"
    if args.check:
        # The release this installation runs, not whatever an older one left in a shared folder.
        folder=current if chosen else configured(args.root)
        if folder is None or chosen and not folder.is_dir():
            print("sdcpp: not installed (optional); run scripts/install-sdcpp.sh")
            return
        try:
            print(json.dumps(verify(folder),indent=2))
        except (OSError,KeyError,ValueError,subprocess.SubprocessError) as problem:
            raise SystemExit(f"sdcpp in {folder} does not work: {problem}") from None
        return
    if args.revision is None:
        if args.source or args.adopt_build:
            parser.error("--source and --adopt-build need the --revision they were built from")
        args.revision=latest_revision()
    flags=build_flags(args.backend,args.cuda_architectures)
    release=release_identity(args.revision,args.backend,flags)
    target=args.prefix/"releases"/release
    print(f"sdcpp: {args.backend}; revision {args.revision}; target {target}")
    if args.dry_run:
        print("Would verify/build sd-server and sd-cli, atomically select the release, and configure settings/loadcoach.toml. Model weights are separate.")
        return
    if not target.exists():
        if not args.adopt_build:
            from engines.build import build_environment
            environment = build_environment(args.backend, "stable-diffusion.cpp")
        args.prefix.mkdir(parents=True,exist_ok=True,mode=0o700)
        target.parent.mkdir(exist_ok=True)
        source=args.source.expanduser().resolve() if args.source else args.prefix/"source"/args.revision
        if not source.exists():
            if args.source:
                raise ValueError("Source checkout does not exist")
            source.parent.mkdir(parents=True,exist_ok=True)
            run(["git","clone","--recursive",REPOSITORY,source])
            run(["git","-C",source,"checkout","--detach",args.revision])
            run(["git","-C",source,"submodule","update","--init","--recursive"])
        revision=run(["git","-C",source,"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
        if revision!=args.revision:
            raise ValueError("Source checkout is not at the selected revision")
        run(["git","-C",source,"diff","--quiet","HEAD","--"])
        modules=run(['git','-C',source,'submodule','status','--recursive'],capture_output=True,text=True).stdout.splitlines()
        if any(not line.startswith(' ') for line in modules):
            raise ValueError('Native submodule revisions are not pinned')
        run(['git','-C',source,'submodule','foreach','--recursive','git diff --quiet HEAD --'])
        build=args.adopt_build.expanduser().resolve() if args.adopt_build else args.prefix/"build"/release
        if not args.adopt_build:
            if args.backend == "cuda":
                flags.append("-DCMAKE_CUDA_COMPILER=" + environment["CUDACXX"])
            run(["cmake","-S",source,"-B",build,*flags], env=environment)
            run(["cmake","--build",build,"--config","Release","--target","sd-cli","sd-server","--parallel",args.jobs], env=environment)
        cache=build/"CMakeCache.txt"
        if not cache.is_file():
            raise ValueError("Build has no CMakeCache.txt provenance")
        cache_text=cache.read_text()
        if args.backend=="cuda" and "SD_CUDA:BOOL=ON" not in cache_text:
            raise ValueError("Selected build does not have the CUDA backend")
        for option in ('SD_WEBM','SD_WEBP'):
            if not re.search(r'^'+option+r':BOOL=ON$',cache_text,re.M):
                raise ValueError('Selected build requires '+option+'=ON; rebuild through scripts/install-sdcpp.sh.')
        with tempfile.TemporaryDirectory(prefix=".stage-",dir=target.parent) as temporary:
            stage=Path(temporary)
            (stage/"bin").mkdir()
            for name in ("sd-server","sd-cli"):
                shutil.copy2(build/"bin"/name,stage/"bin"/name)
            manifest={"version":1,"repository":REPOSITORY,"revision":revision,"backend":args.backend,
                "build_flags":flags,"build_cache_sha256":digest(cache),
                "video_containers":["webm","webp"],
                "executables":{name:digest(stage/"bin"/name) for name in ("sd-server","sd-cli")},
                "split_modes":["layer","row"],"split_note":"Requested modes require model/backend qualification; API flag presence does not prove effective splitting"}
            (stage/"installation.json").write_text(json.dumps(manifest,indent=2)+"\n")
            verify(stage)
            stage.rename(target)
    verify(target)
    link=args.prefix/".current-new"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    link.replace(current)
    configure(args.root/"settings/loadcoach.toml",target/"bin/sd-server",target/"bin/sd-cli",target/"installation.json",args.root/"data/installation/sdcpp-backups")
    print("sdcpp installed and configured for new launches. No service was restarted and no model was downloaded.")


if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,subprocess.SubprocessError) as problem:
        raise SystemExit("sdcpp installation failed: "+str(problem)) from None
