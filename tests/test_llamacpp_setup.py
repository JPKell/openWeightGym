"""Exercise the engine installer without downloading or compiling a real runtime."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).parents[1]


@pytest.fixture
def engine_installer(tmp_path):
    """Run the real entry point and terminal UI with isolated build and download tools."""
    scripts = tmp_path / "install folder/scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install-llamacpp.sh", scripts)
    shutil.copy2(ROOT / "scripts/lib/ui.sh", scripts / "lib")
    (scripts / "lib/detect.sh").write_text("""
detect_os() { OS_ARCH=x86_64; }
detect_gpu() { LLAMA_BACKEND=${TEST_BACKEND:-cpu}; }
shell_path() { printf '%q' "$1"; }
""")
    commands = tmp_path / "commands"
    commands.mkdir()
    for tool in ("bash", "dirname", "mkdir", "date", "chmod", "grep", "sed", "awk", "sort",
                 "wc", "cp", "rm", "mv", "ln", "readlink", "tail", "mktemp", "tar", "gzip", "tr",
                 "stat", "stty", "cut"):
        (commands / tool).symlink_to(shutil.which(tool))
    # The system's env: uv puts a shell snippet called env in ~/.local/bin.
    (commands / "env").symlink_to(shutil.which("env", path="/usr/bin:/bin"))
    for tool in ("c++", "make"):
        (commands / tool).symlink_to(shutil.which("true"))
    server = tmp_path / "llama-server"
    server.write_text("#!/bin/bash\nprintf 'version: fixture\\n'\n")
    server.chmod(0o755)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(server, arcname="bundle/llama-server")
    tools = {
        "git": """printf 'git %s\\n' "$*" >>"$TEST_CALLS"
mkdir -p "${@: -1}/.git"
""",
        "cmake": """printf 'cmake %s\\n' "$*" >>"$TEST_CALLS"
if [[ $1 == --build ]]; then
  [[ ${FAIL_BUILD:-0} != 1 ]] || exit 7
  mkdir -p "$2/bin"
  cp "$TEST_SERVER" "$2/bin/llama-server"
fi
""",
        "ldd": "exit 0\n",
        "nproc": "echo 4\n",
        "curl": """printf 'curl %s\\n' "$*" >>"$TEST_CALLS"
[[ $* != *-fsSIL* ]] || { printf 'Content-Length: 100\\n'; exit 0; }
while (($#)); do
  if [[ $1 == -o ]]; then cp "$TEST_ARCHIVE" "$2"; exit; fi
  shift
done
exit 1
""",
    }
    for name, source in tools.items():
        target = commands / name
        target.write_text("#!/bin/bash\nset -euo pipefail\n" + source)
        target.chmod(0o755)
    prefix = scripts.parent / "runtimes/llamacpp"
    engine = re.search(r"(?ms)^engine\(\) \{.*?^\}", (ROOT / "install.sh").read_text())[0]
    driver = scripts / "engine-step.sh"
    driver.write_text('''#!/bin/bash
set -Eeuo pipefail
DIR=$TEST_INSTALL LLAMA=$TEST_METHOD SYSTEM=0 ASSUME_YES=0 STARTER_REGISTER=1
ui_warn() { printf '%s\\n' "$*"; }
''' + engine + '\nengine\n[[ -x $LLAMA_SERVER ]]\n')
    calls = tmp_path / "calls"
    environment = {**os.environ, "HOME": str(tmp_path), "PATH": str(commands),
                   "OWG_LLAMACPP_BUILD": "b12008", "OWG_DEFAULTS": "0", "OWG_COLOR": "0",
                   "OWG_PLAIN": "1", "OWG_LOG": str(tmp_path / "install.log"),
                   "TEST_CALLS": str(calls), "TEST_SERVER": str(server), "TEST_ARCHIVE": str(archive),
                   "TEST_INSTALL": str(scripts.parent)}

    def run(*args, terminal=False, answer="", env=None, main_mode=None):
        """Optionally give the installer a controlling terminal for its real menu."""
        command = ["/bin/bash", str(scripts / "install-llamacpp.sh"), "--prefix", str(prefix), *args]
        if main_mode:
            command = ["/bin/bash", str(driver)]
        if terminal:
            terminal_tool = shutil.which("script")
            if not terminal_tool:
                pytest.skip("interactive installer checks require util-linux script")
            command = [terminal_tool, "-q", "-e", "-c", shlex.join(command), "/dev/null"]
        return subprocess.run(command, input=answer, text=True, capture_output=True, timeout=15,
                              env={**environment, "OWG_NO_TTY": "0" if terminal else "1",
                                   "TEST_METHOD": main_mode or "", **(env or {})})

    return run, prefix, commands, calls


def test_menu_can_build_after_installing_missing_cmake(engine_installer):
    run, prefix, commands, calls = engine_installer
    assert run("--prebuilt").returncode == 0
    previous = (prefix / "current").resolve()
    cmake = (commands / "cmake").read_text()
    (commands / "cmake").unlink()

    failed = run(terminal=True, answer="2\n")
    assert failed.returncode != 0
    assert "Build here from source" in failed.stdout
    assert "needs: cmake." in failed.stdout
    assert "--from-source again" in failed.stdout
    assert (prefix / "current").resolve() == previous
    assert "git clone" not in calls.read_text()

    (commands / "cmake").write_text(cmake)
    (commands / "cmake").chmod(0o755)
    built = run(terminal=True, answer="2\n")
    assert built.returncode == 0, built.stdout + built.stderr
    assert (prefix / "current").resolve() == prefix / "releases/b12008-cpu-source"
    assert previous.joinpath("llama-server").exists()
    assert "cmake --build" in calls.read_text()

    completed_calls = calls.read_text()
    repeated = run("--from-source", terminal=True)
    assert repeated.returncode == 0 and "already here" in repeated.stdout
    assert "How should llama.cpp be installed?" not in repeated.stdout
    assert calls.read_text() == completed_calls


def test_source_build_resumes_after_compilation_failure(engine_installer):
    run, prefix, commands, calls = engine_installer
    failed = run("--from-source", env={"FAIL_BUILD": "1"})
    assert failed.returncode != 0
    assert not (prefix / "current").exists()
    assert (prefix / "source/b12008/.git").is_dir()

    completed = run("--from-source")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert calls.read_text().count("git clone") == 1
    assert calls.read_text().count("cmake --build") == 2
    assert (prefix / "current/llama-server").is_file()


@pytest.mark.parametrize("options,terminal,answer", [
    ((), False, ""), (("--yes",), False, ""), (("--yes",), True, ""),
    (("--prebuilt",), True, ""), ((), True, "1\n"),
])
def test_prebuilt_defaults_and_explicit_selection(engine_installer, options, terminal, answer):
    run, prefix, commands, calls = engine_installer
    completed = run(*options, terminal=terminal, answer=answer)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (prefix / "current").resolve() == prefix / "releases/b12008-cpu"
    assert "cmake" not in calls.read_text() and "git clone" not in calls.read_text()
    if options:
        assert "How should llama.cpp be installed?" not in completed.stdout


def test_check_does_not_prompt_or_build(engine_installer):
    run, prefix, commands, calls = engine_installer
    completed = run("--check", terminal=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "not installed" in completed.stdout
    assert "How should llama.cpp be installed?" not in completed.stdout
    assert not calls.exists() and not prefix.exists()


@pytest.mark.parametrize("mode", ["auto", "cpu", "source"])
def test_main_installer_keeps_its_chosen_method_without_another_menu(engine_installer, mode):
    run, prefix, commands, calls = engine_installer
    completed = run(terminal=True, main_mode=mode)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "How should llama.cpp be installed?" not in completed.stdout
    release = "b12008-cpu-source" if mode == "source" else "b12008-cpu"
    assert (prefix / "current").resolve() == prefix / "releases" / release
