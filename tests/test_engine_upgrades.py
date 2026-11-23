"""owg upgrade: newer engine releases where they exist, clean reinstalls where they do not."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("engine_upgrades", ROOT / "scripts/engine-upgrades.py")
upgrades = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrades)
SEPARATOR = "\x1f"
SD_REVISION = "1" * 40


def release(prefix, name, manifest=None):
    folder = prefix / "releases" / name
    (folder / "bin").mkdir(parents=True)
    if manifest is not None:
        (folder / "installation.json").write_text(json.dumps(manifest))
    return folder


@pytest.fixture
def installed(tmp_path):
    """An installation running an sdcpp commit, an older audio.cpp and a prebuilt llama.cpp."""
    root = tmp_path / "suite"
    (root / "settings").mkdir(parents=True)
    sd = release(root / "runtimes/sdcpp", f"{SD_REVISION}-cuda-abc", {"revision": SD_REVISION, "backend": "cuda"})
    (root / "runtimes/sdcpp/build" / sd.name).mkdir(parents=True)
    (root / "runtimes/sdcpp/source" / SD_REVISION).mkdir(parents=True)
    audio = release(root / "runtimes/audiocpp", "0" * 40 + "-vulkan", {"revision": "0" * 40, "backend": "vulkan"})
    llama = release(root / "runtimes/llamacpp", "b12008-cuda-13.4")
    (llama / "llama-server").write_text("")
    (root / "runtimes/llamacpp/current").symlink_to("releases/b12008-cuda-13.4")
    (root / "settings/loadcoach.toml").write_text(
        f'llama_server = "{root}/runtimes/llamacpp/current/llama-server"\n\n'
        f'[engines.sdcpp]\nenabled = true\nmanifest = "{sd}/installation.json"\n\n'
        f'[engines.audiocpp]\nenabled = true\nmanifest = "{audio}/installation.json"\n\n'
        '[engines.whispercpp]\nenabled = false\n')
    return root


def by_engine(rows):
    return {row["engine"]: row for row in rows}


def test_plan_upgrades_llama_and_newer_pins_and_reinstalls_the_rest(installed, monkeypatch):
    monkeypatch.setattr(upgrades, "latest_llama", lambda: "v1.0.0")
    # v1.0.0 offers no CUDA 13.4 download: the 12.8 build runs on the same driver.
    monkeypatch.setattr(upgrades, "published", lambda url: "cuda-13.4" not in url)
    monkeypatch.setattr(upgrades, "llama_asset", lambda tag, backend: f"https://example/{tag}/{backend}")
    monkeypatch.setattr(upgrades, "latest_sdcpp", lambda: "2" * 40)
    rows = by_engine(upgrades.plan(installed))
    assert set(rows) == {"llamacpp", "sdcpp", "audiocpp"}  # disabled and absent engines are left out
    assert rows["llamacpp"]["action"] == "upgrade" and rows["llamacpp"]["target"] == "v1.0.0"
    assert rows["llamacpp"]["backend"] == "cuda-12.8" and rows["llamacpp"]["extra"] == "prebuilt"
    assert rows["sdcpp"]["action"] == "upgrade" and rows["sdcpp"]["target"] == "2" * 40  # unpinned: newest upstream commit
    assert rows["audiocpp"]["action"] == "upgrade" and rows["audiocpp"]["backend"] == "vulkan"


def test_plan_follows_the_newest_sdcpp_commit(installed, monkeypatch):
    monkeypatch.setattr(upgrades, "latest_llama", lambda: "b12008")
    monkeypatch.setattr(upgrades, "latest_sdcpp", lambda: SD_REVISION)
    assert by_engine(upgrades.plan(installed))["sdcpp"]["action"] == "current"

    def offline():
        raise OSError("network unreachable")
    monkeypatch.setattr(upgrades, "latest_sdcpp", offline)
    assert by_engine(upgrades.plan(installed))["sdcpp"]["action"] == "unchecked"


def test_plan_leaves_llama_alone_when_current_unchecked_or_your_own(installed, monkeypatch, tmp_path):
    monkeypatch.setattr(upgrades, "latest_llama", lambda: "b12008")
    assert by_engine(upgrades.plan(installed))["llamacpp"]["action"] == "current"

    def offline():
        raise OSError("network unreachable")
    monkeypatch.setattr(upgrades, "latest_llama", offline)
    assert by_engine(upgrades.plan(installed))["llamacpp"]["action"] == "unchecked"
    own = tmp_path / "llama.cpp/build/bin/llama-server"
    own.parent.mkdir(parents=True)
    own.write_text("")
    settings = installed / "settings/loadcoach.toml"
    settings.write_text(f'llama_server = "{own}"\n')
    assert by_engine(upgrades.plan(installed))["llamacpp"]["action"] == "unmanaged"


def test_set_aside_then_restore_or_discard(installed):
    prefix = installed / "runtimes/sdcpp"
    before = sorted(str(path.relative_to(prefix)) for path in prefix.rglob("*"))
    upgrades.set_aside(installed, "sdcpp")
    assert not any(any((prefix / folder).iterdir()) for folder in ("releases", "build", "source"))
    with pytest.raises(ValueError):  # one reinstall at a time
        upgrades.set_aside(installed, "sdcpp")
    # A failed rebuild's leftovers are replaced by the release that worked.
    moved = json.loads(next(prefix.glob(".reinstall-*/moved.json")).read_text())
    (Path(moved[0]["from"]) / "half-built").mkdir(parents=True)
    upgrades.restore(installed, "sdcpp")
    assert sorted(str(path.relative_to(prefix)) for path in prefix.rglob("*")) == before
    upgrades.set_aside(installed, "sdcpp")
    upgrades.discard(installed, "sdcpp")
    assert not list(prefix.glob(".reinstall-*")) and not any((prefix / "releases").iterdir())


def stub_installation(installed, tmp_path, outcome, latest=SD_REVISION, *options):
    """The real driver and helper, with stub build tools, a stub sdcpp installer and a git that names
    `latest` as upstream's newest commit."""
    scripts = installed / "scripts"
    scripts.mkdir()
    for name in ("upgrade-engines.sh", "engine-upgrades.py", "lib"):
        (scripts / name).symlink_to(ROOT / "scripts" / name)
    (installed / ".venv/bin").mkdir(parents=True)
    (installed / ".venv/bin/python").symlink_to(sys.executable)
    (scripts / "install-build-tools.sh").write_text("#!/bin/sh\nexit 0\n")
    (scripts / "install-build-tools.sh").chmod(0o755)
    (scripts / "install-sdcpp.py").write_text(f"""
import json, sys
from pathlib import Path
arguments = sys.argv[1:]
prefix = Path(arguments[arguments.index('--prefix') + 1])
if {outcome!r} == 'fail':
    (prefix / 'releases/half-built').mkdir(parents=True)
    raise SystemExit('sdcpp installation failed: compiler error')
folder = prefix / 'releases/rebuilt'
folder.mkdir(parents=True)
(folder / 'installation.json').write_text(json.dumps({{'revision': 'rebuilt', 'arguments': arguments}}))
""")
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "git").write_text(f"#!/bin/sh\n[ \"$1\" = ls-remote ] || exit 1\nprintf '%s\\tHEAD\\n' {latest}\n")
    (tools / "git").chmod(0o755)
    environment = dict(os.environ, OWG_LOG=str(tmp_path / "upgrade.log"), OWG_NO_TTY="1", OWG_PLAIN="1",
                       XDG_STATE_HOME=str(tmp_path / "state"), PATH=f"{tools}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(scripts / "upgrade-engines.sh"), "--dir", str(installed), "--yes", *options, "sdcpp"],
                          capture_output=True, text=True, env=environment, timeout=60)


def test_driver_reinstalls_cleanly_and_deletes_the_old_release(installed, tmp_path):
    old = next((installed / "runtimes/sdcpp/releases").iterdir())
    result = stub_installation(installed, tmp_path, "pass", SD_REVISION, "--reinstall")
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = installed / "runtimes/sdcpp"
    assert not old.exists() and not list(prefix.glob(".reinstall-*"))
    arguments = json.loads((prefix / "releases/rebuilt/installation.json").read_text())["arguments"]
    assert arguments[arguments.index("--backend") + 1] == "cuda"
    assert arguments[arguments.index("--revision") + 1] == SD_REVISION
    assert "owg restart loadcoach" in result.stdout


def test_driver_builds_the_newest_sdcpp_commit_beside_the_current_release(installed, tmp_path):
    old = next((installed / "runtimes/sdcpp/releases").iterdir())
    result = stub_installation(installed, tmp_path, "pass", "2" * 40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert old.exists() and "111111111111 → 222222222222" in result.stdout
    arguments = json.loads((installed / "runtimes/sdcpp/releases/rebuilt/installation.json").read_text())["arguments"]
    assert arguments[arguments.index("--revision") + 1] == "2" * 40


def test_driver_puts_the_old_release_back_when_the_rebuild_fails(installed, tmp_path):
    old = next((installed / "runtimes/sdcpp/releases").iterdir())
    manifest = (old / "installation.json").read_bytes()
    result = stub_installation(installed, tmp_path, "fail", SD_REVISION, "--reinstall")
    assert result.returncode == 1
    assert (old / "installation.json").read_bytes() == manifest
    assert "previous release is back in use" in result.stdout
    assert not list((installed / "runtimes/sdcpp").glob(".reinstall-*"))


def test_check_changes_nothing_and_unknown_engines_are_refused(installed, tmp_path):
    scripts = installed / "scripts"
    stub_installation(installed, tmp_path, "pass", "2" * 40)  # prepares the stubs and names a newer commit
    environment = dict(os.environ, OWG_LOG=str(tmp_path / "check.log"), OWG_NO_TTY="1", OWG_PLAIN="1",
                       PATH=f"{tmp_path / 'tools'}:{os.environ['PATH']}")  # the stub git: no network
    before = sorted(map(str, (installed / "runtimes").rglob("*")))
    checked = subprocess.run(["bash", str(scripts / "upgrade-engines.sh"), "--dir", str(installed), "--check", "sdcpp"],
                             capture_output=True, text=True, env=environment, timeout=60)
    assert checked.returncode == 0 and "Nothing was changed (--check)" in checked.stdout
    assert sorted(map(str, (installed / "runtimes").rglob("*"))) == before
    refused = subprocess.run(["bash", str(scripts / "upgrade-engines.sh"), "--dir", str(installed), "--check", "comfy"],
                             capture_output=True, text=True, env=environment, timeout=60)
    assert refused.returncode == 2


def test_owg_names_upgrade():
    owg = (ROOT / "scripts/owg").read_text()
    assert 'upgrade) exec "$ROOT/scripts/upgrade-engines.sh" --dir "$ROOT" "$@"' in owg
    assert "Upgrade engines|" in owg
