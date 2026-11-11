"""Persisted native versions and hourly refreshes without launching model workers."""

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from loadcoach.component_versions import EVERY, Versions, git_version, reported_version, update_installation
from test_setup_recovery import ROOT, one_function, shell


def program(tmp_path, name, output, status=0):
    """A version-only native program that records every invocation."""
    path = tmp_path / name
    path.write_text(f'#!/bin/sh\n[ "$1" = --version ] || exit 99\necho called >> "{tmp_path / "calls"}"\n'
                    f"printf '%s\\n' '{output}'\nexit {status}\n")
    path.chmod(0o755)
    return str(path)


def test_suite_identity_is_nearest_tag_and_actual_commit(tmp_path):
    """A commit after a release must still show the release and current revision."""
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-qm", "release")
    git("tag", "v1.2.3")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-qm", "next")
    assert git_version(tmp_path) == f'v1.2.3 ({git("rev-parse", "--short=12", "HEAD")})'


def test_versions_survive_reopen_and_refresh_only_after_sixty_minutes(tmp_path, monkeypatch):
    executable = program(tmp_path, "llama-server", "backend: CUDA runtime version 12\nversion: 1234 (abcd)")
    config = SimpleNamespace(engines={}, llama_server=executable)
    now = [10000]
    monkeypatch.setattr("loadcoach.component_versions.time.time", lambda: now[0])
    versions = Versions(tmp_path / "versions.db")
    asyncio.run(versions.refresh(config))
    assert versions.read()["llama_cpp"]["version"] == "1234 (abcd)"
    versions.db.close()
    versions = Versions(tmp_path / "versions.db")
    now[0] += EVERY - 1
    asyncio.run(versions.refresh(config))
    assert (tmp_path / "calls").read_text().splitlines() == ["called"]
    now[0] += 1
    asyncio.run(versions.refresh(config))
    assert len((tmp_path / "calls").read_text().splitlines()) == 2
    report = {"services": [{"id": "loadcoach", "installed": True}, {"id": "spotter", "installed": True}],
              "engines": [{"id": "llama_cpp", "installed": True}]}
    asyncio.run(versions.attach(report))
    assert report["services"][0]["version"] == report["services"][1]["version"] == versions.read()["suite"]["version"]
    assert report["engines"][0]["version"] == "1234 (abcd)"
    assert len((tmp_path / "calls").read_text().splitlines()) == 2  # rendering never runs an engine
    versions.db.close()


def test_failed_probe_is_unknown_and_does_not_prevent_other_engines(tmp_path):
    broken = program(tmp_path, "llama-server", "unsupported option", status=1)
    audio = program(tmp_path, "audiocpp_server", "audio.cpp v0.9.2 (abcdef)")
    config = SimpleNamespace(engines={"audiocpp": {"enabled": False, "executable": audio}}, llama_server=broken)
    versions = Versions(tmp_path / "versions.db")
    versions.save("llama_cpp", "previous binary")
    asyncio.run(versions.refresh(config, force=True))
    assert not versions.read()["llama_cpp"]["version"]
    assert "status 1" in versions.read()["llama_cpp"]["version_error"]
    assert versions.read()["audiocpp"]["version"] == "audio.cpp v0.9.2 (abcdef)"
    versions.db.close()


def test_installation_forces_refresh_in_configured_store(tmp_path):
    (tmp_path / "settings").mkdir()
    executable = program(tmp_path, "llama-server", "version: b999 (abcd)")
    (tmp_path / "settings/loadcoach.toml").write_text(f'llama_server = "{executable}"\ndatabase = "{tmp_path / "core.db"}"\n')
    asyncio.run(update_installation(tmp_path))
    asyncio.run(update_installation(tmp_path))
    versions = Versions(tmp_path / "core.db")
    assert versions.read()["llama_cpp"]["version"] == "b999 (abcd)"
    assert len((tmp_path / "calls").read_text().splitlines()) == 2
    versions.db.close()


def test_probe_rejects_unbounded_output(tmp_path):
    executable = program(tmp_path, "verbose-engine", "x" * 17000)
    with pytest.raises(ValueError, match="too long"):
        asyncio.run(reported_version(executable))


@pytest.mark.parametrize("dry_run", [False, True])
def test_suite_installer_records_versions_after_engine_installation(tmp_path, dry_run):
    install = tmp_path / "installed suite"
    (install / "settings").mkdir(parents=True)
    executable = program(tmp_path, "llama-server", "version: b2026 (abcd)")
    (install / "settings/loadcoach.toml").write_text(
        f'llama_server = "{executable}"\ndatabase = "{tmp_path / "core.db"}"\n')
    source = (ROOT / "scripts/install-suite.sh").read_text()
    hook = source.split("# Save versions after optional engines are installed", 1)[1].split("\nrender_unit()", 1)[0]
    hook = "\n# Save versions after optional engines are installed" + hook
    # Run the actual installer hook and its dry-run guard, without provisioning services.
    answer = shell(tmp_path, '''
set -euo pipefail
ROOT=$INSTALL PY=$TEST_PYTHON DRY_RUN=$TEST_DRY_RUN
ui_step() { shift 2; "$@"; }
run() { :; }
die() { printf '%s\\n' "$@" >&2; exit 1; }
''' + one_function("scripts/install-suite.sh", "try_step") + hook,
        env={"INSTALL": str(install), "TEST_PYTHON": sys.executable, "TEST_DRY_RUN": str(int(dry_run))})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert (tmp_path / "core.db").exists() is not dry_run
    assert (tmp_path / "calls").exists() is not dry_run
    if not dry_run:
        versions = Versions(tmp_path / "core.db")
        assert versions.read()["llama_cpp"]["version"] == "b2026 (abcd)"
        versions.db.close()
