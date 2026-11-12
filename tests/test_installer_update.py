"""Code updates select current source without resuming setup or rebuilding engines."""
import importlib.util
from io import BytesIO
import json
import os
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from test_setup_recovery import ROOT, one_function, script_functions, shell


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, text=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "install.sh").write_text("#!/bin/bash\n# fixture checkout\n")
    (origin / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (origin / "code.txt").write_text("old code\n")
    git("init", "-b", "main", origin)
    git("-C", origin, "config", "user.name", "Update fixture")
    git("-C", origin, "config", "user.email", "fixture@example.invalid")
    git("-C", origin, "add", ".")
    git("-C", origin, "commit", "-m", "initial")
    installed = tmp_path / "installed suite"
    git("clone", origin, installed)
    (origin / "code.txt").write_text("new code\n")
    git("-C", origin, "commit", "-am", "update")
    return installed


def run_update(tmp_path, installed, *, menu=False, env=None):
    source = script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
source "$TEST_REPO/scripts/lib/receipt.sh"
load_ui() { CHECKOUT=$INSTALL; }
forbidden() { printf 'Unexpected install action: %s\\n' "$*" >&2; exit 91; }
warm_up() { forbidden warm_up; }
interrupted_menu() { forbidden interrupted_menu; }
resume_settings() { forbidden resume_settings; }
reinstall() { forbidden reinstall; }
apply_components() { forbidden apply_components; }
loading_plates() { forbidden loading_plates; }
setting_up() { forbidden setting_up; }
write_receipt() { forbidden write_receipt; }
'''
    if menu:
        source += '''
parse_arguments --dir "$INSTALL"
ui_init
receipt_load "$INSTALL"
installed_menu
'''
    else:
        source += 'main --update --dir "$INSTALL" --yes --strata install\n'
    return shell(tmp_path, source, env={"INSTALL": str(installed), **(env or {})})


@pytest.mark.parametrize("state,operation", [("complete", "install"), ("pending", "install"),
                                           ("pending", "components"), ("legacy", "")])
def test_update_pulls_code_and_leaves_pending_engines_and_other_state_alone(tmp_path, checkout, state, operation):
    retained = {
        "settings/loadcoach.toml": 'llama_server = "/installed/server"\n',
        "runtimes/strata/partial-build": "keep failed build\n",
        "runtimes/llamacpp/current/llama-server": "keep installed engine\n",
        "data/records": "keep data\n",
    }
    if state != "legacy":
        retained[".install-receipt"] = (f"state={state}\noperation={operation}\nmode=user\n"
            "topology=compute\nservices=loadcoach,spotter,voice\nstrata=install\nstrata_host_ram_mib=20000\n"
            "sdcpp=install\naudiocpp=install\n")
    for name, content in retained.items():
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    answer = run_update(tmp_path, checkout)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert (checkout / "code.txt").read_text() == "new code\n"
    assert all((checkout / name).read_text() == content for name, content in retained.items())
    assert not (checkout / ".venv").exists()
    if state == "pending":
        assert "unfinished installation is still saved" in answer.stdout
    elif state == "legacy":
        assert not (checkout / ".install-receipt").exists()


def test_installed_menu_update_does_not_reinstall(tmp_path, checkout):
    answer = run_update(tmp_path, checkout, menu=True)
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert (checkout / "code.txt").read_text() == "new code\n"


def voice_download_sources(checkout):
    """Publish the real packager inputs through the fixture's normal Git update."""
    origin = checkout.parent / 'origin'
    sources = ['scripts/package-voice-desktop.py']
    sources += [f'voice/{name}.py' for name in ('__init__', 'desktop', 'desktop_audio', 'echo', 'protocol')]
    sources += [f'desktop/{name}' for name in ('requirements.txt', 'README.txt', 'Voice.cmd', 'Voice.command')]
    for name in sources:
        target = origin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    git('-C', origin, 'add', '.')
    git('-C', origin, 'commit', '-m', 'publish desktop client')
    interpreter = checkout / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    settings = checkout / 'settings/weightroom.toml'
    settings.parent.mkdir()
    settings.write_text('data = "../data"\n')


def test_update_builds_missing_and_stale_voice_download_from_installed_source(tmp_path, checkout):
    voice_download_sources(checkout)
    destination = checkout / 'weightroom/static/LoadCoach-Voice.zip'
    for old_archive in (None, b'stale archive'):
        if old_archive is not None:
            destination.write_bytes(old_archive)
        answer = run_update(tmp_path, checkout)
        assert answer.returncode == 0, answer.stdout + answer.stderr
        if old_archive is not None:
            assert 'Already up to date' in answer.stdout
        app = Starlette(routes=[Mount('/static', StaticFiles(directory=destination.parent))])
        with TestClient(app) as client:
            download = client.get('/static/LoadCoach-Voice.zip')
        assert download.status_code == 200 and download.headers['content-type'] == 'application/zip'
        with ZipFile(BytesIO(download.content)) as archive:
            assert archive.testzip() is None
            expected = {f'LoadCoach-Voice/voice/{name}' for name in (
                '__init__.py', 'desktop.py', 'desktop_audio.py', 'echo.py', 'protocol.py')}
            expected |= {f'LoadCoach-Voice/{name}' for name in (
                'requirements.txt', 'README.txt', 'Voice.cmd', 'Voice.command')}
            assert set(archive.namelist()) == expected
            assert archive.read('LoadCoach-Voice/voice/desktop.py') == (checkout / 'voice/desktop.py').read_bytes()
            assert archive.getinfo('LoadCoach-Voice/Voice.command').external_attr >> 16 == 0o100755


def test_update_reports_packaging_failure_instead_of_success(tmp_path, checkout):
    voice_download_sources(checkout)
    destination = checkout / 'weightroom/static/LoadCoach-Voice.zip'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'previous download')
    origin = checkout.parent / 'origin'
    (origin / 'voice/desktop.py').unlink()
    git('-C', origin, 'commit', '-am', 'simulate missing desktop source')
    answer = run_update(tmp_path, checkout)
    assert answer.returncode != 0
    assert 'Voice desktop download could not be built' in answer.stderr
    assert 'Code update complete' not in answer.stdout
    assert destination.read_bytes() == b'previous download'
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize('service,dry_run', [('weightroom', False), ('weightroom', True), ('loadcoach', False)])
def test_install_and_repair_build_voice_download_only_for_console(tmp_path, checkout, service, dry_run):
    voice_download_sources(checkout)
    git('-C', checkout, 'pull', '--ff-only')
    suite = (ROOT / 'scripts/install-suite.sh').read_text()
    start = suite.index("\nif has_service weightroom; then\n  try_step 'Building the Voice desktop download'")
    end = suite.index('\nmkdir_if_real()', start)
    source = '''set -euo pipefail
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
ROOT=$INSTALL DRY_RUN=$TEST_DRY_RUN
has_service() { [[ $1 == "$TEST_SERVICE" ]]; }
die() { ui_die "$@"; }
''' + one_function('scripts/install-suite.sh', 'quote_command') + '\n' + \
        one_function('scripts/install-suite.sh', 'run') + '\n' + \
        one_function('scripts/install-suite.sh', 'try_step') + suite[start:end]
    answer = shell(tmp_path, source, env={'INSTALL': str(checkout), 'TEST_SERVICE': service,
                                         'TEST_DRY_RUN': str(int(dry_run))})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert (checkout / 'weightroom/static/LoadCoach-Voice.zip').exists() == (service == 'weightroom' and not dry_run)
    if dry_run:
        assert 'DRY RUN:' in answer.stdout and 'package-voice-desktop.py' in answer.stdout


@pytest.mark.parametrize("failure", ["local-edits", "unreachable-origin", "not-a-checkout"])
def test_failed_update_does_not_resume_and_retry_is_also_code_only(tmp_path, checkout, failure):
    receipt = checkout / ".install-receipt"
    saved = "state=pending\noperation=components\nstrata=install\n"
    receipt.write_text(saved)
    if failure == "local-edits":
        (checkout / "code.txt").write_text("user edits\n")
    elif failure == "unreachable-origin":
        git("-C", checkout, "remote", "set-url", "origin", tmp_path / "missing-origin")
    else:
        (checkout / ".git").rename(checkout / "retained-git")
    answer = run_update(tmp_path, checkout)
    assert answer.returncode != 0
    assert "--update" in answer.stderr and "Run the code update again:" in answer.stderr
    assert "Unexpected install action" not in answer.stderr
    assert receipt.read_text() == saved
    assert (checkout / "code.txt").read_text() == ("user edits\n" if failure == "local-edits" else "old code\n")


def test_bootstrap_update_retry_preserves_update_flag(tmp_path):
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
source "$TEST_REPO/scripts/lib/detect.sh"
parse_arguments --update --dir "$INSTALL"
set_retry
printf '%s\\n' "$OWG_RETRY_COMMAND" "$OWG_RETRY"
''', env={"INSTALL": str(tmp_path / "missing")})
    assert answer.returncode == 0, answer.stderr
    assert "| bash -s -- --dir" in answer.stdout and "--update" in answer.stdout
    assert "Run the code update again:" in answer.stdout


def source_helper():
    spec = importlib.util.spec_from_file_location('runtime_source_update', ROOT / 'scripts/update-runtime-source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installed_unit(tmp_path, root, program='weightroom'):
    units = tmp_path / 'config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    (units / f'{program}.service').write_text(f'[Service]\nExecStart="{root}/.venv/bin/{program}" --config settings.toml\n')
    dropins = units / f'{program}.service.d'
    dropins.mkdir(exist_ok=True)
    return units, dropins


def test_source_update_preserves_custom_libraries_other_policies_and_rollback(tmp_path, monkeypatch):
    root = tmp_path / 'installed suite'
    units, dropins = installed_unit(tmp_path, root)
    pin = dropins / 'zz-source.conf'
    original = '[Service]\nEnvironment="CUDA_VISIBLE_DEVICES=0" "PYTHONPATH=/old/release:/custom/libs"\nMemoryMax=12G\n'
    pin.write_text(original)
    policy = dropins / 'resources.conf'
    policy.write_text('[Service]\nCPUQuota=200%\nEnvironment="LD_LIBRARY_PATH=/cuda/lib"\n')
    helper = source_helper()
    calls = []
    monkeypatch.setattr(helper.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    changes = helper.plan(root, units, ['weightroom'])
    assert list(changes) == [pin]
    helper.apply(changes, tmp_path / 'recovery')
    assert f'"PYTHONPATH={root}:/old/release:/custom/libs"' in pin.read_text()
    assert '"CUDA_VISIBLE_DEVICES=0"' in pin.read_text() and 'MemoryMax=12G' in pin.read_text()
    assert policy.read_text() == '[Service]\nCPUQuota=200%\nEnvironment="LD_LIBRARY_PATH=/cuda/lib"\n'
    assert calls == [['systemctl','--user','daemon-reload']]
    manifest = next((tmp_path / 'recovery').glob('*/manifest.json'))
    saved = manifest.parent / json.loads(manifest.read_text())[str(pin)]
    assert saved.read_text() == original and saved.stat().st_mode & 0o777 == 0o600
    assert helper.plan(root, units, ['weightroom']) == {}


def test_environment_file_cannot_restore_an_old_pin(tmp_path):
    root = tmp_path / 'installed'
    units, dropins = installed_unit(tmp_path, root)
    environment = tmp_path / 'runtime.env'
    environment.write_text('PRIVATE_TOKEN=do-not-print\nPYTHONPATH="/old release:/vendor"\n')
    (dropins / 'environment.conf').write_text(f'[Service]\nEnvironmentFile="{environment}"\n')
    changes = source_helper().plan(root, units, ['weightroom'])
    assert changes[environment][1] == f'PRIVATE_TOKEN=do-not-print\nPYTHONPATH="{root}:/old release:/vendor"\n'


def test_foreign_units_are_untouched_and_unsupported_commands_fail_before_writing(tmp_path):
    root = tmp_path / 'installed'
    units, dropins = installed_unit(tmp_path, tmp_path / 'different-installation')
    pin = dropins / 'source.conf'
    pin.write_text('[Service]\nEnvironment="PYTHONPATH=/older"\n')
    helper = source_helper()
    assert helper.plan(root, units, ['weightroom']) == {}
    (units / 'weightroom.service').write_text(f'[Service]\nExecStart={root}/.venv/bin/weightroom\n')
    command = dropins / 'zz-command.conf'
    command.write_text('[Service]\nExecStart=\nExecStart=/old/.venv/bin/weightroom\n')
    with pytest.raises(ValueError, match='outside this checkout'):
        helper.plan(root, units, ['weightroom'])
    assert pin.read_text() == '[Service]\nEnvironment="PYTHONPATH=/older"\n'


def test_failed_reload_restores_source_override(tmp_path, monkeypatch):
    root = tmp_path / 'installed'
    units, dropins = installed_unit(tmp_path, root)
    pin = dropins / 'source.conf'
    original = '[Service]\nEnvironment="PYTHONPATH=/old"\n'
    pin.write_text(original)
    helper = source_helper()
    def failed(command, **kwargs):
        if kwargs.get('check'):
            raise subprocess.CalledProcessError(1,command)
    monkeypatch.setattr(helper.subprocess,'run',failed)
    with pytest.raises(subprocess.CalledProcessError):
        helper.apply(helper.plan(root,units,['weightroom']),tmp_path/'recovery')
    assert pin.read_text() == original


def test_restart_selects_source_before_restarting_services(tmp_path):
    source = 'source "$TEST_REPO/scripts/lib/ui.sh"\nui_init\n' + one_function('scripts/owg','control') + '''
ROOT="$INSTALL"
refresh_runtime_sources() { printf 'source:%s\\n' "$*" >> "$SOURCE_CALLS"; }
owg_systemctl() { printf 'service:%s\\n' "$*" >> "$SOURCE_CALLS"; }
control restart weightroom turnstile
'''
    calls = tmp_path / 'restart-calls'
    answer = shell(tmp_path,source,env={'INSTALL':str(tmp_path/'installed'),'SOURCE_CALLS':str(calls)})
    assert answer.returncode == 0, answer.stderr
    lines = calls.read_text().splitlines()
    assert lines == [f'source:{tmp_path}/installed weightroom turnstile',
                     'service:restart weightroom.service','service:restart turnstile.service']


def test_restart_stops_if_source_selection_fails(tmp_path):
    source = 'source "$TEST_REPO/scripts/lib/ui.sh"\nui_init\n' + one_function('scripts/owg','control') + '''
ROOT="$INSTALL"
refresh_runtime_sources() { return 1; }
owg_systemctl() { printf 'UNEXPECTED SERVICE RESTART\\n'; }
control restart weightroom
'''
    answer = shell(tmp_path,source,env={'INSTALL':str(tmp_path/'installed')})
    assert answer.returncode == 1
    assert 'UNEXPECTED SERVICE RESTART' not in answer.stdout


def test_update_selects_checkout_even_when_git_is_already_current(tmp_path, checkout):
    interpreter = checkout / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    helper = checkout / 'scripts/update-runtime-source.py'
    helper.parent.mkdir()
    shutil.copyfile(ROOT / 'scripts/update-runtime-source.py',helper)
    _, dropins = installed_unit(tmp_path,checkout)
    pin = dropins / 'zz-source.conf'
    tools = tmp_path / 'tools'
    tools.mkdir()
    systemctl = tools / 'systemctl'
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SOURCE_CALLS"\n')
    systemctl.chmod(0o755)
    calls = tmp_path / 'source-calls'
    env = {'PATH':str(tools)+os.pathsep+os.environ['PATH'], 'SOURCE_CALLS':str(calls)}
    for index in range(2):
        pin.write_text('[Service]\nEnvironment="PYTHONPATH=/old/release"\n')
        answer = run_update(tmp_path,checkout,env=env)
        assert answer.returncode == 0, answer.stdout + answer.stderr
        assert f'PYTHONPATH={checkout}:/old/release' in pin.read_text()
        if index:
            assert 'Already up to date' in answer.stdout
    assert calls.read_text().splitlines() == ['--user daemon-reload','--user daemon-reload']
