"""Code updates never resume setup, rebuild engines or mutate installation state."""
import subprocess

import pytest

from test_setup_recovery import script_functions, shell


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


def run_update(tmp_path, installed, *, menu=False):
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
    return shell(tmp_path, source, env={"INSTALL": str(installed)})


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
