"""Real fake CLI and loopback adapter fixtures for the independently installed protocol."""

import json
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from loadcoach.runner import free_port
from plugins._registry import Registry
from plugins._runtime import build_app
from plugins._sdk import atomic_json

SOURCE = Path(__file__).resolve().parents[1] / "plugins"

CLI = '''#!/usr/bin/env python3
import json,os,sys,time,subprocess,signal
from pathlib import Path
a=sys.argv[1:]
capture=Path(__file__).with_suffix('.capture')
def emit(x):print(json.dumps(x),flush=True)
if a[:2]==['profile','list']:
    print('Notice: stale dashboard process')
    print('  Profile              Model              Gateway   Alias')
    print('  ◆ default            qwen/test          running   -')
    print('    Gandalf (gandalf)   qwen/test          running   -')
    sys.exit()
if 'gateway' in a:
    print('Gateway is running')
    with capture.open('a') as f:f.write(json.dumps({'argv':a})+'\\n')
    sys.exit()
q=Path(a[a.index('--query-file')+1]).read_text()
with capture.open('a') as f:f.write(json.dumps({'argv':a,'query':q})+'\\n')
print('Notice: startup diagnostic',flush=True)
if '--resume' in a:print('[Conversation resumed]',flush=True)
emit({'type':'system','subtype':'init','session_id':'native-123'})
print('Hindsight is down: customization diagnostic',flush=True)
if q=='FAIL':
    emit({'type':'result','text':'','exit_code':1})
    sys.exit(1)
if q=='BLOCK':
    child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
    Path(__file__).with_suffix('.pid').write_text(str(child.pid))
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    time.sleep(60)
emit({'type':'text','text':'Intermediate thought'})
emit({'type':'reasoning','text':'Available reasoning'})
emit({'type':'tool_use','name':'read_file','input':{'path':'hello'},'usage':{'tokens':88}})
emit({'type':'tool_result','output':'<script>unsafe</script>','tokens':99})
emit({'type':'result','text':'Final answer','session_id':'native-123','exit_code':0,'tokens':{'total_tokens':123}})
'''


def setup(tmp_path):
    """Copy only Hermes resources and configure a private selected instance."""
    root = tmp_path / "plugins"
    shutil.copytree(SOURCE / "hermes", root / "hermes", ignore=shutil.ignore_patterns('__pycache__', 'settings.json', '.settings.lock'))
    registry = Registry(root, tmp_path / "data")
    cli = tmp_path / "fake-hermes"
    cli.write_text(CLI)
    cli.chmod(0o700)
    settings = {"cli": str(cli), "run_budget": 5}
    registry.save_instance("hermes", "local", "http://adapter", settings, enabled=True)
    registry.enable("hermes", True)
    config = tmp_path / "adapter/settings.json"
    atomic_json(config, {"plugin": "hermes", "instance": "local", "revision": 1, "settings": settings,
                        "policy": {"allow_start": False, "allow_restart": False}, "key_file": "turnstile.key"})
    (config.parent / "turnstile.key").write_text(registry.pairing_key("hermes", "local"))
    app = build_app(config, registry.manifest("hermes"))
    headers = {"authorization": "Bearer " + registry.pairing_key("hermes", "local"), "x-suite-instance": "local",
               "x-suite-user-id": "person-one", "x-suite-model": "hermes:gandalf"}
    return registry, cli, app, headers


@contextmanager
def serving(app):
    """Host a real streaming HTTP app with graceful lifespan and process cleanup."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive()


def turns(cli):
    """Read argv/query evidence produced by the fake CLI, without credentials."""
    file = cli.with_suffix('.capture')
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []
