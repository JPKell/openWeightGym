#!/usr/bin/env python3
"""Qualify a local video profile under the existing external-work lock and a transient RAM cap.

The installed checkout, settings and stores are never changed. The worker calls Mirror Wall's
production Native/video workflow, records per-stage telemetry, cancellation and crash cleanup,
then checks Chrome/Firefox playback. Owner visual acceptance is explicitly left pending.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def write(path,value):
    """Publish bounded machine-readable qualification evidence atomically."""
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    temporary.replace(path)


def playback(directory):
    """Check the actual completed WebM in both browser engines over local HTTP."""
    from playwright.sync_api import sync_playwright
    class Quiet(SimpleHTTPRequestHandler):
        """Serve only the qualification directory without verbose request logs."""
        def log_message(self,*args):
            """Suppress local playback access logs; no prompts or credentials are served."""
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(directory)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    (directory/'playback.html').write_text('<video src="clip.webm" controls muted autoplay loop></video>')
    results=[]
    try:
        with sync_playwright() as driver:
            for name in ('chromium','firefox'):
                browser=getattr(driver,name).launch(headless=True)
                try:
                    page=browser.new_page()
                    page.goto('http://127.0.0.1:'+str(server.server_port)+'/playback.html')
                    page.wait_for_function('document.querySelector("video").currentTime > 0.05',timeout=30000)
                    row=page.eval_on_selector('video','v=>({width:v.videoWidth,height:v.videoHeight,duration:v.duration,error:v.error?.message || null,playing:!v.paused})')
                    results.append({'browser':name,**row,'passed':bool(row['width'] and row['playing'] and not row['error'])})
                finally:
                    browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
    return results


async def benchmark_queue(args):
    """Read the configured control-node queue without initializing or changing its schema."""
    code=f'''from pathlib import Path
import sys,tomllib
root=Path({str(args.benchmark_root)!r});sys.path.insert(0,str(root))
from db import DatabaseRegistry
settings=root/'settings/weightroom.toml';value=tomllib.loads(settings.read_text())
registry=DatabaseRegistry.from_config(value.get('databases',{{}}),settings.parent,{{'weightroom.benchmarks':root/'data/benchmarks.db'}})
source=registry.database('weightroom.benchmarks')
with source.connect() as connection:
 connection.execute('BEGIN READ ONLY' if source.engine=='postgresql' else 'PRAGMA query_only=ON')
 print(connection.execute("SELECT COUNT(*) FROM benchmark_runs WHERE state IN ('queued','running')").fetchone()[0])
'''
    import shlex
    child=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',args.benchmark_host,
        shlex.quote(str(args.benchmark_root/'.venv/bin/python')),'-',stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,_=await asyncio.wait_for(child.communicate(code.encode()),20)
    if child.returncode or out.strip()!=b'0':
        raise RuntimeError('Control-node benchmarks are active or their queue could not be established; no GPU work started.')


async def outside(args):
    """Drain-check and lease the owning node; renew until every qualification child exits."""
    import httpx
    await benchmark_queue(args)
    client=httpx.AsyncClient(base_url=args.suite_url,trust_env=False,timeout=20,
        headers={'authorization':'Bearer '+args.suite_key_file.read_text().strip(),'x-turnstile-no-wake':'1'})
    token=None;child=None
    async def ask(method,path,body=None):
        """Use the installed API's current authority; never print credentials."""
        answer=await client.request(method,path,json=body,headers={'x-loadcoach-lock':token} if token else {})
        if answer.status_code>=400:
            raise RuntimeError(f'{path} returned HTTP {answer.status_code}')
        return answer.json()
    try:
        state=await ask('GET','/loadcoach/status')
        if state.get('hostname')!=socket.gethostname() or not {'busy','running','waiting','batches','lock','draining'}<=state.keys():
            raise RuntimeError('The owning host or complete work state could not be established.')
        if any(state.get(key) for key in ('busy','running','waiting','batches','active_batches','lock','draining','holds','others_running','others_waiting')):
            raise RuntimeError('The installed node is busy; qualification did not take resources.')
        args.output.mkdir(parents=True,exist_ok=False)
        token=(await ask('POST','/loadcoach/lock',{'seconds':90,'unload':True}))['token']
        command=['systemd-run','--user','--wait','--pipe','--collect','--quiet','-p','Delegate=memory',
                 '-p','DelegateSubgroup=supervisor','-p','MemoryMax=18G','-p','MemorySwapMax=0',
                 '--working-directory='+str(ROOT),sys.executable,str(Path(__file__).resolve()),'--inside',
                 '--output',str(args.output),'--runtime',str(args.runtime),'--weights',str(args.weights),
                 '--card',str(args.card),'--benchmark-host',args.benchmark_host,'--benchmark-root',str(args.benchmark_root)]
        command+=['--scenarios',*args.scenarios]
        if args.profile:
            command+=['--profile',str(args.profile)]
        child=await asyncio.create_subprocess_exec(*command,start_new_session=True)
        while child.returncode is None:
            try:
                await asyncio.wait_for(child.wait(),10)
            except TimeoutError:
                await ask('POST','/loadcoach/lock',{'seconds':90})
                state=await ask('GET','/loadcoach/status')
                if state.get('waiting') or state.get('others_waiting'):
                    raise RuntimeError('Foreground work is waiting; qualification yields the node.')
        return child.returncode
    finally:
        if child and child.returncode is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:
                await asyncio.wait_for(child.wait(),20)
            except TimeoutError:
                os.killpg(child.pid,signal.SIGKILL);await child.wait()
        if token:
            await ask('POST','/loadcoach/unlock')
        await client.aclose()


async def inside(args):
    """Exercise the actual owned video runtime, preserving evidence for every terminal outcome."""
    if os.getsid(0)!=os.getpid():
        os.setsid()
    os.environ['CUDA_VISIBLE_DEVICES']=str(args.card)
    from mirrorwall.runtime import Native,video_workflow
    from mirrorwall.telemetry import Meter
    from mirrorwall.install import verify,digest
    from mirrorwall.components import check
    from mirrorwall.engine import build_identity
    from mirrorwall.video_media import prepare
    report={'started':time.time(),'owner_visual_acceptance':'pending','checks':[],
            'revision':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()}
    settings=json.loads(args.profile.read_text()) if args.profile else {
        'engine':'sdcpp','alone':True,'gpus':1,'flags':{'model':str(args.weights/'wan2.1_t2v_1.3B_fp16.safetensors'),'ctx-size':0},
        'sdcpp':{'mode':'vid_gen','family':'wan2.1','components':{'vae':str(args.weights/'wan_2.1_vae.safetensors'),
            'text_encoder':str(args.weights/'umt5-xxl-encoder-Q5_K_M.gguf')},'host_ram_mib':16384,'staged':True,
            'vram_limit_gib':[14.5],'limits':{'max_job_seconds':1800}}}
    installation={'enabled':True,'executable':str(args.runtime/'bin/sd-server'),'cli':str(args.runtime/'bin/sd-cli'),
                  'manifest':str(args.runtime/'installation.json')}
    report['installation']=verify(args.runtime)
    report['build']=build_identity(installation)
    report['profile']=settings
    report['component_sha256']={key:await asyncio.to_thread(digest,Path(path)) for key,path in
        {'diffusion_model':settings['flags']['model'],**settings['sdcpp']['components']}.items()}
    report['components']=check(settings,str(args.weights),[str(args.weights)])
    if not report['components']['ready']:
        write(args.output/'report.json',report)
        raise ValueError('Profile components are incomplete; inspect report.json.')
    uuid=subprocess.check_output(['nvidia-smi','-i',str(args.card),'--query-gpu=uuid','--format=csv,noheader'],text=True).strip()
    request={'model':'qualification-video','prompt':'A small red wooden sailboat gently moving across a calm blue lake, natural daylight.',
             'size':'832x480','frames':33,'fps':16,'steps':20,'seed':42}
    for scenario in args.scenarios:
        try:
            await benchmark_queue(args)
        except RuntimeError as problem:
            report['interrupted_reason']=str(problem)
            report['not_started']=scenario
            write(args.output/'report.json',report)
            return 1
        with tempfile.TemporaryDirectory(prefix='video-qualification-') as scratch:
            job={'settings':settings,'installation':installation,'cards':['CUDA0'],'scratch':scratch,
                 'qualification_log':str(args.output/(scenario+'.log'))}
            native=Native(job);meter=Meter([uuid]);sampling=asyncio.create_task(meter.run());process_id=None
            began=time.monotonic();outcome={'scenario':scenario,'passed':False}
            sequence=video_workflow(job,request,native)
            try:
                async for event in sequence:
                    meter.stage=event.get('stage',meter.stage)
                    if native.process:
                        process_id=native.process.pid
                    if event.get('stage','').startswith('sampling') and scenario!='smoke':
                        if scenario=='crash':
                            native.process.kill();await native.process.wait()
                            try:
                                await anext(sequence)
                            except Exception:
                                pass
                        break
                    if event.get('type')=='video_generation.completed':
                        raw,metadata=await asyncio.to_thread(prepare,base64.b64decode(event['b64_json']),
                            {key:value for key,value in event.items() if key not in ('b64_json','type')})
                        (args.output/'clip.webm').write_bytes(raw)
                        if poster:=metadata.pop('poster_b64',None):
                            (args.output/'poster.png').write_bytes(base64.b64decode(poster))
                        report['result']=metadata
                        outcome['passed']=True
            except Exception as problem:
                outcome['error']=str(problem)
            finally:
                await sequence.aclose();await native.stop();meter.stopping.set();await sampling
                outcome.update(elapsed_seconds=time.monotonic()-began,telemetry=meter.result(),
                               native_exited=not process_id or not Path('/proc/'+str(process_id)).exists())
            if scenario!='smoke':
                outcome['passed']=bool(process_id and outcome['native_exited'] and native.process is None)
            report['checks'].append(outcome);write(args.output/'report.json',report)
            print(scenario,json.dumps({k:v for k,v in outcome.items() if k!='telemetry'}),flush=True)
            if not outcome['passed']:
                return 1
    report['browser_playback']=await asyncio.to_thread(playback,args.output) if 'smoke' in args.scenarios else []
    report['completed']=time.time();write(args.output/'report.json',report)
    return 0 if all(one['passed'] for one in report['browser_playback']) else 1


def main():
    """Run only explicit qualification targets, retaining failed output for diagnosis."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--weights',type=Path,required=True)
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--suite-url',default='http://127.0.0.1:8442')
    parser.add_argument('--suite-key-file',type=Path)
    parser.add_argument('--benchmark-host',required=True)
    parser.add_argument('--benchmark-root',type=Path,default=Path('/home/owgadmin/openWeightGym'))
    parser.add_argument('--card',type=int,default=0)
    parser.add_argument('--inside',action='store_true')
    parser.add_argument('--scenarios',nargs='+',choices=('smoke','cancel','crash'),default=['smoke','cancel','crash'],
                        help='Run selected checks into a fresh evidence directory; default runs all three.')
    args=parser.parse_args()
    for key in ('output','runtime','weights','profile','suite_key_file'):
        value=getattr(args,key)
        if value:setattr(args,key,value.expanduser().resolve())
    if not args.inside and not args.suite_key_file:
        parser.error('The owning node API key file is required.')
    raise SystemExit(asyncio.run(inside(args) if args.inside else outside(args)))


if __name__=='__main__':
    main()
