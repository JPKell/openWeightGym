"""Disposable real-service cluster; no host installs, credentials or model folders."""
from pathlib import Path
import secrets
import subprocess
import os
import sys
import tomllib

import uvicorn

from loadcoach.config import model_places

NODES = ('alpha', 'beta', 'gamma')


def base(name):
    return Path('/state') / name


def places(name):
    return dict(model_places(str(base(name) / 'cache'), {}, str(base(name) / 'storage')))


def private_file(path, text):
    path.write_text(text)
    path.chmod(0o600)


def delegate_memory():
    """Delegate only this container's finite cgroup; leave host/service policies alone."""
    root = Path('/sys/fs/cgroup')
    membership = Path('/proc/self/cgroup').read_text().strip()
    if membership != '0::/':
        raise RuntimeError('Qualification needs a private container cgroup namespace.')
    if (root / 'memory.max').read_text().strip() != str(16 * 2**30):
        raise RuntimeError('Qualification needs its configured 16 GiB container memory ceiling.')
    if (root / 'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('Qualification requires container swap disabled.')
    subprocess.run(['mount', '-o', 'remount,rw', '/sys/fs/cgroup'], check=True)
    supervisor = root / 'supervisor'
    supervisor.mkdir(exist_ok=True)
    for process in (root / 'cgroup.procs').read_text().split():
        (supervisor / 'cgroup.procs').write_text(process)
    (root / 'cgroup.subtree_control').write_text('+memory')


def seed():
    from loadcoach.groups import Group
    from loadcoach.users import Users
    key_folder = base('front') / 'keys'
    key_folder.mkdir(parents=True, exist_ok=True)
    identity = key_folder / 'qualification-recovery.agekey'
    if not identity.exists():
        subprocess.run(['age-keygen', '-o', str(identity)], check=True, capture_output=True)
    recipient = subprocess.check_output(['age-keygen', '-y', str(identity)], text=True).strip()
    for node in ('front', *NODES):
        folder = base(node) / 'backups'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_file(folder / 'recipients.txt', recipient + '\n')
    if (base('front') / 'qualification-session.key').exists():
        return
    for name in ('front', *NODES):
        base(name).mkdir(parents=True, exist_ok=True)
        for folder in places(name).values():
            Path(folder).mkdir(parents=True, exist_ok=True)
    users = Users(str(base('front') / 'accounts.db'), {
        'operators': Group(can=frozenset({'superuser'})),
        'console': Group(can=frozenset({'follow-settings', 'sign-in', 'mirror-users'})),
        'members': Group(can=frozenset({'sign-in'})),
    })
    users.add('operator', 'operators')
    users.add('weightroom', 'console')
    users.add('member', 'members')
    private_file(base('front') / 'console.key', users.new_key('weightroom'))
    private_file(base('front') / 'qualification-session.key', users.new_key('operator', hours=168))
    for name in NODES:
        key = secrets.token_urlsafe(40)
        private_file(base('front') / (name + '.key'), key)
        private_file(base(name) / 'machine.key', key)


def application(role, name):
    databases = ({'stores': {'qualification_pg': {'engine': 'postgresql', 'dsn_file': '/state/front/qualification-postgres.dsn'}},
                  'bindings': {'weightroom.accounts': 'qualification_pg'}}
                 if os.environ.get('OWG_QUALIFICATION_POSTGRES') == '1' else {})
    if role in ('loadcoach', 'spotter'):
        identity = base(name) / 'identity'
        identity.mkdir(exist_ok=True)
        link = Path.home() / '.local/share/suite'
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(identity, target_is_directory=True)
    if role == 'turnstile':
        from turnstile.app import Settings, build_app
        settings = Settings(data=str(base('front')), host='0.0.0.0',
            ports=(8442, 8443, 8444, 8445),
            databases=databases,
            machine_key_file=str(base('front') / 'alpha.key'),
            auth_key_file=str(base('front') / 'console.key'), machines_poll_seconds=2,
            keep_console_settings_on_front=True, toollocker_url='http://toollocker:8443',
            models_folder=str(base('front') / 'cache'), models_storage_folder=str(base('front') / 'storage'),
            machines={node: {'compute': f'http://{node}:8442', 'spotter': f'http://{node}:8444',
                             'machine_key_file': str(base('front') / (node + '.key')),
                             **({'formerly': 'compute'} if node == 'alpha' else {})}
                      for node in NODES},
        )
        from loadcoach.settings import said
        import json
        from dataclasses import asdict
        (base('front') / 'settings-seen-turnstile.json').write_text(json.dumps(said(asdict(settings), {}, {})))
        return build_app(settings), 8442
    if role == 'console':
        from weightroom.app import Settings, build_app
        return build_app(Settings(compute='http://alpha:8442', door='http://turnstile',
            databases=databases,
            key=(base('front') / 'console.key').read_text(), data=str(base('front')),
            spotter='http://alpha:8444', host='0.0.0.0', secure=False,
            voice_gateway='http://voice-gateway:8446', timezone='America/Vancouver')), 8440
    if role == 'voice':
        from voice.service import build_app
        settings = tomllib.loads(Path('/app/settings/defaults/voice.toml').read_text())
        root = Path('/opt/owg-runtimes/voice')
        recognition = root / 'sherpa-onnx-streaming-zipformer-en-20M-2023-02-17'
        synthesis = root / 'kokoro-int8-multi-lang-v1_1'
        settings.update(compute='http://turnstile:8442',
                        key_file='/state/front/qualification-session.key',
                        machine_key_file=str(base(name) / 'machine.key'), data=str(base(name)))
        settings['profiles']['default'].update(model='qualification-fleet', max_context_tokens=2048, max_output_tokens=64)
        settings['recognition'] = {key: str(recognition / file) for key, file in {
            'encoder': 'encoder-epoch-99-avg-1.int8.onnx', 'decoder': 'decoder-epoch-99-avg-1.int8.onnx',
            'joiner': 'joiner-epoch-99-avg-1.int8.onnx', 'tokens': 'tokens.txt'}.items()}
        settings['synthesis'] = {**{key: str(synthesis / file) for key, file in {
            'model': 'model.int8.onnx', 'voices': 'voices.bin', 'tokens': 'tokens.txt',
            'data_dir': 'espeak-ng-data', 'lexicon': 'lexicon-us-en.txt'}.items()}, 'workers': 1, 'threads': 4}
        return build_app(settings), 8445
    if role == 'voice-gateway':
        from voice.gateway import build_app
        settings = tomllib.loads(Path('/app/settings/defaults/voice-gateway.toml').read_text())
        settings.update(turnstile='http://turnstile:8442', voice_turnstile='http://turnstile:8445',
                        voice='http://alpha:8445', key_file='/state/front/qualification-session.key',
                        browser_key_file='/state/front/qualification-session.key', data=str(base('front')),
                        origins=['http://127.0.0.1:19440'], max_active_sessions=2)
        return build_app(settings), 8446
    if role == 'loadcoach':
        delegate_memory()
        from loadcoach.app import build_app
        from loadcoach.config import Config
        from loadcoach.devices import discover
        gpu = os.environ.get('OWG_QUALIFICATION_GPU') == '1'
        executable = '/opt/owg-runtimes/' + ('llamacpp-gpu' if gpu else 'llamacpp') + '/current/llama-server'
        cards = discover(executable, include_integrated=False) if gpu else []
        return build_app(Config(machine_name=name, former_machine_name='compute' if name == 'alpha' else '',
            machine_key_file=str(base(name) / 'machine.key'), front='http://turnstile:8442',
            database=str(base(name) / 'loadcoach.db'), token_file=str(base(name) / 'loadcoach.token'),
            data=str(base(name)), host='0.0.0.0', places=places(name),
            llama_server=executable, gpus=tuple(card['id'] for card in cards), gpu_details=cards,
            engines={'llama_cpp': {'enabled': True, 'executable': executable},
                     'sdcpp': {'enabled': True, 'executable': '/opt/owg-runtimes/sdcpp/current/bin/sd-server',
                               'cli': '/opt/owg-runtimes/sdcpp/current/bin/sd-cli'},
                     'audiocpp': {'enabled': True, 'executable': '/opt/owg-runtimes/audiocpp/current/bin/audiocpp_server',
                                  'cli': '/opt/owg-runtimes/audiocpp/current/bin/audiocpp_cli',
                                  'manifest': '/opt/owg-runtimes/audiocpp/current/installation.json'}},
            nvidia_smi='nvidia-smi' if gpu else 'qualification-cpu-only', host_ram_limit_mib=16384,
        )), 8442
    if role == 'toollocker':
        from toollocker.door import Config, build_app
        return build_app(Config(servers={'files': {'command': [sys.executable, '-m', 'toollocker.tools', '/app/toollocker/tools'],
                                                   'description': 'Disposable qualification files and code tools'}},
                               machine_key_file=str(base('front') / 'alpha.key'),
                               database=str(base('front') / 'accounts.db'), host='0.0.0.0')), 8443
    if role == 'spotter':
        from spotter.app import Settings, build_app
        return build_app(Settings(services={'voice': 'voice.service'} if name in ('alpha', 'beta') else {}, compute=f'http://{name}:8442', host='0.0.0.0',
            machine_key_file=str(base(name) / 'machine.key'), places=places(name),
            token_file=str(base(name) / 'loadcoach.token'), database=str(base(name) / 'loadcoach.db'),
            data=str(base(name)), history=str(base(name) / 'spotter.db'),
        )), 8444
    raise ValueError('Unknown qualification service')


if __name__ == '__main__':
    role = sys.argv[1]
    if role == 'seed':
        seed()
    else:
        app, port = application(role, sys.argv[2] if len(sys.argv) > 2 else '')
        if role == 'turnstile':
            from turnstile.app import listening
            uvicorn.Server(uvicorn.Config(app, access_log=False)).run(
                sockets=[listening('0.0.0.0', one) for one in (8442, 8443, 8444, 8445)])
        else:
            uvicorn.run(app, host='0.0.0.0', port=port, access_log=False)
