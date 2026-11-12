"""Disposable real-service cluster; no host installs, credentials or model folders."""
from pathlib import Path
import secrets
import sys

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


def seed():
    from loadcoach.groups import Group
    from loadcoach.users import Users
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
    if role == 'turnstile':
        from turnstile.app import Settings, build_app
        # Until the peer-only configuration lands, alpha occupies the legacy entry.
        return build_app(Settings(compute='alpha', data=str(base('front')), host='0.0.0.0',
            machine_key_file=str(base('front') / 'alpha.key'),
            auth_key_file=str(base('front') / 'console.key'), machines_poll_seconds=2,
            keep_console_settings_on_front=True,
            models_folder=str(base('front') / 'cache'), models_storage_folder=str(base('front') / 'storage'),
            machines={node: {'compute': f'http://{node}:8442', 'spotter': f'http://{node}:8444',
                             'machine_key_file': str(base('front') / (node + '.key'))}
                      for node in NODES if node != 'alpha'},
        )), 8442
    if role == 'console':
        from weightroom.app import Settings, build_app
        return build_app(Settings(compute='http://alpha:8442', door='http://turnstile',
            key=(base('front') / 'console.key').read_text(), data=str(base('front')),
            spotter='http://alpha:8444', host='0.0.0.0', secure=False,
            timezone='America/Vancouver')), 8440
    if role == 'loadcoach':
        from loadcoach.app import build_app
        from loadcoach.config import Config
        return build_app(Config(machine_name='compute' if name == 'alpha' else name,
            machine_key_file=str(base(name) / 'machine.key'), front='http://turnstile:8442',
            database=str(base(name) / 'loadcoach.db'), token_file=str(base(name) / 'loadcoach.token'),
            data=str(base(name)), host='0.0.0.0', places=places(name),
            llama_server='/opt/owg-runtimes/llamacpp/current/llama-server',
            engines={'llama_cpp': {'enabled': True, 'executable': '/opt/owg-runtimes/llamacpp/current/llama-server'}},
            nvidia_smi='qualification-cpu-only', host_ram_limit_mib=8192,
        )), 8442
    if role == 'spotter':
        from spotter.app import Settings, build_app
        return build_app(Settings(services={}, compute=f'http://{name}:8442', host='0.0.0.0',
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
                sockets=[listening('0.0.0.0', one) for one in (8442, 8443, 8444)])
        else:
            uvicorn.run(app, host='0.0.0.0', port=port, access_log=False)
