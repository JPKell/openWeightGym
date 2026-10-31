"""Build a private, standalone Linux adapter installer from an explicit source list."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from plugins._registry import Registry


INSTALL = '''#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec python3 install.py "$@"
'''

SCRIPT = '''"""Install this adapter's runtime and user service while preserving existing state."""
import argparse,json,os,shutil,subprocess
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--directory',type=Path)
p.add_argument('--no-service',action='store_true',help='Install without registering/starting systemd.')
args=p.parse_args()
source=Path(__file__).resolve().parent
initial=json.loads((source/'initial-settings.json').read_text())
root=(args.directory or Path.home()/'.local/share/suite-adapters'/initial['plugin']/initial['instance']).expanduser().resolve()
root.mkdir(parents=True,exist_ok=True,mode=0o700)
os.chmod(root,0o700)
shutil.copytree(source/'plugins',root/'plugins',dirs_exist_ok=True)
shutil.copytree(source/'db',root/'db',dirs_exist_ok=True)
shutil.copytree(source/'loadcoach',root/'loadcoach',dirs_exist_ok=True)
shutil.copy2(source/'pyproject.toml',root/'pyproject.toml')
for directory in ('keys','data'):
    (root/directory).mkdir(exist_ok=True,mode=0o700)
for original,target in [('initial-settings.json','settings.json'),('turnstile.key','keys/turnstile.key')]:
    destination=root/target
    if not destination.exists():
        descriptor=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'wb') as output:output.write((source/original).read_bytes())
python=root/'.venv/bin/python'
if not python.exists():subprocess.run(['python3','-m','venv',str(root/'.venv')],check=True)
subprocess.run([str(python),'-m','pip','install','-q',str(root)],check=True)
if not args.no_service:
    units=Path.home()/'.config/systemd/user'
    units.mkdir(parents=True,exist_ok=True)
    name='suite-adapter-'+initial['plugin']+'-'+initial['instance']+'.service'
    def quote(value):return json.dumps(str(value).replace('%','%%'))
    unit='[Unit]\\nDescription=openWeightGym '+initial['plugin']+' adapter '+initial['instance']+'\\nAfter=network-online.target\\n\\n[Service]\\nWorkingDirectory='+str(root).replace('%','%%')+'\\nExecStart='+quote(python)+' -m plugins._runtime --config '+quote(root/'settings.json')+'\\nRestart=on-failure\\nRestartSec=3\\nUMask=0077\\n\\n[Install]\\nWantedBy=default.target\\n'
    (units/name).write_text(unit)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now',name],check=True)
    print('Installed '+name+'. Existing running services are not restarted by reinstallation.')
print('Adapter settings: '+str(root/'settings.json'))
'''


def bundle(registry: Registry, plugin: str, instance: str) -> bytes:
    """Package only runtime code, this plugin and its deliberate pairing configuration."""
    manifest, config = registry.manifest(plugin), registry.instance(plugin, instance)
    initial = {"plugin": plugin, "instance": instance, "revision": config["revision"],
               "settings": config["settings"], "key_file": "keys/turnstile.key", "policy": {
                   "allow_start": config["health"]["allow_start"], "allow_restart": config["health"]["allow_restart"]}}
    metadata = '''[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
[project]
name = "suite-adapter-runtime"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["starlette", "uvicorn", "httpx", "jsonschema==4.26.0"]
[project.scripts]
suite-adapter = "plugins._runtime:main"
[tool.setuptools]
packages = ["plugins", "db", "db.drivers", "loadcoach"]
[tool.setuptools.package-data]
plugins = ["**/*"]
'''
    shared = Path(__file__).parent
    readme = (shared / "INSTALLER-README.md").read_text().replace("@PLUGIN@", plugin).replace("@INSTANCE@", instance)
    files = {"install.sh": INSTALL.encode(), "install.py": SCRIPT.encode(), "README.md": readme.encode(),
             "pyproject.toml": metadata.encode(),
             "initial-settings.json": (json.dumps(initial, indent=2) + "\n").encode(),
             "turnstile.key": (registry.pairing_key(plugin, instance) + "\n").encode()}
    for name in ("__init__.py", "_sdk.py", "_runtime.py"):
        files["plugins/" + name] = (shared / name).read_bytes()
    database = shared.parent / "db"
    for name in ("__init__.py", "config.py", "database.py", "connection.py", "migrations.py", "schemas.py"):
        files["db/" + name] = (database / name).read_bytes()
    for name in ("__init__.py", "sqlite.py", "postgresql.py"):
        files["db/drivers/" + name] = (database / "drivers" / name).read_bytes()
    # Ship only the shared observation/signature/storage dependencies, never openWeightGym settings.
    for name in ('__init__.py', 'observability.py', 'observation_store.py', 'log.py', 'statement.py',
                 'client_origin.py', 'users.py', 'groups.py', 'machine_identity.py', 'usage.py'):
        files['loadcoach/'+name] = (shared.parent/'loadcoach'/name).read_bytes()
    resources = [manifest.folder / "manifest.json", manifest.driver,
                 manifest.folder / json.loads((manifest.folder / "manifest.json").read_text())["schema"],
                 manifest.folder / "README.md", *manifest.files]
    files["plugins/" + plugin + "/__init__.py"] = b'"""Installed external adapter plugin."""\n'
    for path in resources:
        if path.is_file():
            files["plugins/" + plugin + "/" + str(path.relative_to(manifest.folder))] = path.read_bytes()
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, value in files.items():
            entry = tarfile.TarInfo("suite-" + plugin + "-" + instance + "/" + name)
            entry.size = len(value)
            entry.mode = 0o700 if name == "install.sh" else 0o600
            archive.addfile(entry, io.BytesIO(value))
    return result.getvalue()
