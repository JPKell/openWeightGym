"""Disposable GPU development cluster using existing qualification service wiring."""

import importlib.util
from pathlib import Path
import sys

import uvicorn

spec = importlib.util.spec_from_file_location('qualification', Path(__file__).parents[1] / 'qualification/service.py')
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)
qualification.NODES = ('alpha',)


if __name__ == '__main__':
    role = sys.argv[1]
    if role == 'seed':
        qualification.seed()
        from loadcoach.users import Users
        users = Users('/state/front/accounts.db', {})
        if not users.by_name('operator').group.can:
            raise RuntimeError('Qualification operator is unavailable.')
        # Test-only browser password and keys never come from a host installation.
        users.change('operator', password='extraction-docker-test')
    elif role == 'browser':
        import runpy
        runpy.run_path(str(Path(__file__).with_name('browser.py')), run_name='__main__')
    else:
        if role == 'loadcoach':
            runtime = Path('/runtime/llamacpp-gpu')
            runtime.mkdir(exist_ok=True)
            if not (runtime / 'current').exists():
                (runtime / 'current').symlink_to('/runtime/build/bin', target_is_directory=True)
        if role == 'toollocker':
            from toollocker.door import Config, build_app
            app, port = build_app(Config(servers={}, machine_key_file='/state/front/alpha.key',
                database='/state/front/accounts.db', host='0.0.0.0', extraction_url='http://turnstile:8442')), 8443
        else:
            app, port = qualification.application(role, sys.argv[2] if len(sys.argv) > 2 else '')
        if role == 'turnstile':
            from turnstile.app import listening
            uvicorn.Server(uvicorn.Config(app, access_log=False)).run(sockets=[listening('0.0.0.0', one) for one in (8442, 8443, 8444, 8445)])
        else:
            uvicorn.run(app, host='0.0.0.0', port=port, access_log=False)
