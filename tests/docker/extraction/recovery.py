"""Crash/recovery qualification for the disposable GPU cluster, driven from its host."""

import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ['docker', 'compose', '-f', str(Path(__file__).with_name('compose.yaml'))]


def inside(code, service='turnstile'):
    return subprocess.check_output([*COMPOSE, 'exec', '-T', service, 'python', '-'], input=code, text=True)


CLIENT = '''
from pathlib import Path
import httpx,json,time
c=httpx.Client(base_url='http://127.0.0.1:8442',headers={'authorization':'Bearer '+Path('/state/front/qualification-session.key').read_text().strip()},timeout=30)
'''


if __name__ == '__main__':
    created = json.loads(inside(CLIENT + '''
jobs=c.get('/owg/extractions').json()['data']
source=next(job for job in jobs if job['asset'] and job['asset']['name']=='receipt.png')
r=c.post('/owg/extractions',json={'asset_id':source['asset_id'],'model':'extraction-gemma','task':'describe','detail':'detailed','output_format':'json'})
r.raise_for_status();job=r.json()
for attempt in range(100):
 job=c.get('/owg/extractions/'+job['id']).json()
 if job['status']=='running':break
 time.sleep(.05)
assert job['status']=='running',job
print(json.dumps({'id':job['id'],'status':job['status'],'asset':job['asset_id']}))
'''))
    subprocess.run([*COMPOSE, 'kill', '-s', 'SIGKILL', 'turnstile'], check=True)
    subprocess.run([*COMPOSE, 'up', '-d', '--no-deps', 'turnstile'], check=True)
    recovered = None
    for attempt in range(60):
        try:
            recovered = json.loads(inside(CLIENT + f'''
r=c.get('/owg/extractions/{created['id']}');r.raise_for_status();job=r.json()
print(json.dumps({{'id':job['id'],'status':job['status'],'error':job['error'],'asset_status':c.get('/owg/extraction-assets/'+job['asset_id']).status_code}}))
'''))
            break
        except subprocess.CalledProcessError:
            time.sleep(.5)
    assert recovered and recovered['status'] == 'failed' and recovered['error']['code'] == 'interrupted', recovered
    assert recovered['asset_status'] == 200
    retried = json.loads(inside(CLIENT + f'''
job=c.get('/owg/extractions/{created['id']}').json()
r=c.post('/owg/extractions',json={{**job['spec'],'retry_of':job['id']}});r.raise_for_status();retry=r.json()
for attempt in range(240):
 retry=c.get('/owg/extractions/'+retry['id']).json()
 if retry['status'] not in ('queued','running'):break
 time.sleep(.5)
assert retry['status'] in ('completed','needs_review'),retry
print(json.dumps({{'id':retry['id'],'status':retry['status'],'provenance':retry['provenance'],'usage':retry['usage']}}))
'''))
    assert retried['id'] != created['id']
    runtime = json.loads(inside('''
from pathlib import Path
import hashlib,json,subprocess,loadcoach.app,engines.llama_cpp.adapter
files=[Path('/runtime/build/bin')/name for name in ('llama-server','libllama-server-impl.so','libllama.so','libggml-cuda.so')]
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as source:
  while chunk:=source.read(8*1024*1024):h.update(chunk)
 return h.hexdigest()
print(json.dumps({'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=index,name,memory.used,utilization.gpu','--format=csv,noheader'],text=True).splitlines(),
 'modules':[loadcoach.app.__file__,engines.llama_cpp.adapter.__file__],
 'binary_sha256':{str(path):digest(path) for path in files}}))
''', 'alpha'))
    import hashlib
    feature_paths = ['weightroom/extraction.py','weightroom/extraction_store.py','weightroom/extraction_views.py',
        'turnstile/extractions.py','turnstile/responses.py','toollocker/extraction.py','toollocker/door.py',
        'turnstile/app.py','turnstile/roles.py','turnstile/accounts.py','turnstile/hosted.py',
        'weightroom/app.py','weightroom/navigation.py','weightroom/pages/extraction.html','weightroom/pages/extraction_parts.html',
        'weightroom/static/extraction.js','weightroom/static/extraction.css','db/schemas.py','engines/llama_cpp/adapter.py']
    evidence = {'before_crash':created,'after_restart':recovered,'explicit_retry':retried,'runtime':runtime,
                'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in feature_paths}}
    output = ROOT/'build/extraction-live/recovery.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2))
    print(json.dumps({'interruption':recovered['error']['code'],'retry':retried['status'],'evidence':str(output)}))
