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
    shutil.copy2(ROOT / "scripts/lib/detect.sh", scripts / "lib")
    shutil.copy2(ROOT / "scripts/lib/build.sh", scripts / "lib")
    shutil.copy2(ROOT / "scripts/install-build-tools.sh", scripts)
    with (scripts / "lib/detect.sh").open("a") as detection:
        detection.write("""
detect_os() { OS_ARCH=x86_64; OS_FAMILY=${TEST_OS_FAMILY:-other}; }
detect_gpu() { LLAMA_BACKEND=${TEST_BACKEND:-cpu}; }
shell_path() { printf '%q' "$1"; }
""")
    # GPU toolkit discovery must never pick up a real compiler on the test host.
    installer = scripts / "lib/build.sh"
    installer.write_text(installer.read_text().replace("/opt/cuda", str(tmp_path / "system/cuda"))
                         .replace("/usr/local/cuda", str(tmp_path / "system/local-cuda"))
                         .replace("/opt/rocm", str(tmp_path / "system/rocm")))
    commands = tmp_path / "commands"
    commands.mkdir()
    for tool in ("bash", "dirname", "mkdir", "date", "chmod", "grep", "sed", "awk", "sort",
                 "wc", "cp", "rm", "mv", "ln", "readlink", "tail", "mktemp", "tar", "gzip", "tr",
                 "stat", "stty", "cut", "id", "true", "touch"):
        (commands / tool).symlink_to(shutil.which(tool))
    # The system's env: uv puts a shell snippet called env in ~/.local/bin.
    (commands / "env").symlink_to(shutil.which("env", path="/usr/bin:/bin"))
    for tool in ("cc", "c++", "make"):
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
        "pkg-config": """[[ $1 == --exists ]] || exit 1
shift
for library; do [[ ! -e $TEST_REQUIREMENTS/$library ]] || exit 1; done
""",
        "sudo": """printf 'sudo %s\\n' "$*" >>"$TEST_CALLS"
[[ $1 != -n ]] || shift
[[ $1 != chown ]] || exit 0
exec "$@"
""",
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
DIR=$TEST_INSTALL LLAMA=$TEST_METHOD SYSTEM=${TEST_SYSTEM:-0} ASSUME_YES=0 SVC=fixture
source "$DIR/scripts/lib/ui.sh"
source "$DIR/scripts/lib/detect.sh"
ui_init
''' + engine + '''
engine
if [[ ${EXPECT_ENGINE:-1} == 1 ]]; then
  [[ -f $LLAMA_SERVER && -x $LLAMA_SERVER ]]
else
  [[ -z $LLAMA_SERVER ]]
fi
''')
    calls = tmp_path / "calls"
    requirements = tmp_path / "missing-requirements"
    requirements.mkdir()
    environment = {**os.environ, "HOME": str(tmp_path), "PATH": str(commands),
                   "OWG_LLAMACPP_BUILD": "b12008", "OWG_DEFAULTS": "0", "OWG_COLOR": "0",
                   "OWG_PLAIN": "1", "OWG_LOG": str(tmp_path / "install.log"),
                   "TEST_CALLS": str(calls), "TEST_SERVER": str(server), "TEST_ARCHIVE": str(archive),
                   "TEST_INSTALL": str(scripts.parent), "TEST_COMMANDS": str(commands),
                   "TEST_REQUIREMENTS": str(requirements), "TEST_SUPPLY": str(tmp_path / "supplied-tools"),
                   "CUDACXX": "", "CUDAToolkit_ROOT": "", "CUDA_PATH": "", "CUDA_HOME": "",
                   "ROCM_PATH": "", "HIP_PATH": ""}

    def run(*args, terminal=False, answer="", env=None, main_mode=None, toolchain=None):
        """Optionally give the installer a controlling terminal for its real menu."""
        command = ["/bin/bash", str(scripts / "install-llamacpp.sh"), "--prefix", str(prefix), *args]
        if main_mode:
            command = ["/bin/bash", str(driver)]
        if toolchain:
            command = ["/bin/bash", str(scripts / "install-build-tools.sh"), *toolchain]
        if terminal:
            terminal_tool = shutil.which("script")
            if not terminal_tool:
                pytest.skip("interactive installer checks require util-linux script")
            command = [terminal_tool, "-q", "-e", "-c", shlex.join(command), "/dev/null"]
        return subprocess.run(command, input=answer, text=True, capture_output=True, timeout=15,
                              env={**environment, "OWG_NO_TTY": "0" if terminal else "1",
                                   "TEST_METHOD": main_mode or "", **(env or {})})

    run.environment = environment
    return run, prefix, commands, calls


@pytest.fixture
def supply_build_packages(engine_installer, tmp_path):
    """Package managers supply fixture tools and headers, never change the real machine."""
    _, _, commands, _ = engine_installer
    supplied = tmp_path / "supplied-tools"
    supplied.mkdir()
    for tool in ("git", "cmake", "cc", "c++", "make", "pkg-config"):
        shutil.copy2(commands / tool, supplied / tool)
    for tool in ("nvcc", "glslc", "hipcc"):
        (supplied / tool).write_text("#!/bin/bash\nexit 0\n")
        (supplied / tool).chmod(0o755)

    def manager(name="pacman"):
        target = commands / name
        target.write_text('''#!/bin/bash
set -euo pipefail
printf 'packages %s %s\\n' "${0##*/}" "$*" >>"$TEST_CALLS"
[[ $* != *update* ]] || exit 0
[[ ${FAIL_PACKAGES:-0} != 1 ]] || exit 8
[[ ${SKIP_SUPPLY:-0} != 1 ]] || exit 0
cp -a --remove-destination "$TEST_SUPPLY/." "$TEST_COMMANDS/"
rm -f "$TEST_REQUIREMENTS/"*
if [[ -n ${TEST_CUDA_ROOT:-} ]]; then
  mkdir -p "$TEST_CUDA_ROOT/bin"
  mv "$TEST_COMMANDS/nvcc" "$TEST_CUDA_ROOT/bin/nvcc"
fi
cuda_root=${TEST_CUDA_ROOT:-${TEST_COMMANDS%/*}}
mkdir -p "$cuda_root/include" "$cuda_root/lib64"
touch "$cuda_root/include/cuda_runtime.h" "$cuda_root/include/cublas_v2.h" "$cuda_root/lib64/libcudart.so" "$cuda_root/lib64/libcublas.so"
if [[ -n ${ROCM_PATH:-} ]]; then
  mkdir -p "$ROCM_PATH/bin" "$ROCM_PATH/lib/cmake/hipblas" "$ROCM_PATH/lib/cmake/rocblas"
  mv "$TEST_COMMANDS/hipcc" "$ROCM_PATH/bin/hipcc"
  touch "$ROCM_PATH/lib/cmake/hipblas/hipblas-config.cmake" "$ROCM_PATH/lib/cmake/rocblas/rocblas-config.cmake"
fi
''')
        target.chmod(0o755)
        return supplied

    return manager


def test_menu_installs_missing_cmake_and_resumes_after_package_failure(engine_installer, supply_build_packages):
    run, prefix, commands, calls = engine_installer
    assert run("--prebuilt").returncode == 0
    previous = (prefix / "current").resolve()
    supply_build_packages()
    (commands / "cmake").unlink()

    failed = run(terminal=True, answer="2\n", env={"FAIL_PACKAGES": "1"})
    assert failed.returncode != 0
    assert "Build here from source" in failed.stdout
    assert "Could not install llama.cpp's build requirements" in failed.stdout
    assert (prefix / "current").resolve() == previous
    assert "git clone" not in calls.read_text()

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


@pytest.mark.parametrize("system", ["0", "1"])
@pytest.mark.parametrize("mode", ["auto", "cpu", "source"])
def test_main_installer_runs_when_a_path_setup_script_shadows_env(engine_installer, system, mode):
    run, prefix, commands, calls = engine_installer
    (commands / "env").unlink()
    (commands / "env").write_text('#!/bin/sh\nexport PATH="$HOME/.local/bin:$PATH"\n')
    (commands / "env").chmod(0o755)

    completed = run(main_mode=mode, env={"TEST_SYSTEM": system})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    release = "b12008-cpu-source" if mode == "source" else "b12008-cpu"
    assert (prefix / "current").resolve() == prefix / "releases" / release
    expected_command = "cmake --build" if mode == "source" else "curl"
    assert expected_command in calls.read_text()


@pytest.mark.parametrize("output", ["missing", "not-executable", "directory", "failed"])
def test_main_installer_does_not_configure_an_engine_that_was_not_installed(engine_installer, output):
    run, prefix, commands, calls = engine_installer
    installer = prefix.parents[1] / "scripts/install-llamacpp.sh"
    installer.write_text("#!/bin/bash\nexit 7\n" if output == "failed" else "#!/bin/bash\nexit 0\n")
    executable = prefix / "current/llama-server"
    if output in {"not-executable", "directory"}:
        executable.parent.mkdir(parents=True)
        if output == "directory":
            executable.mkdir()
        else:
            executable.write_text("#!/bin/bash\nexit 0\n")

    completed = run(main_mode="source", env={"EXPECT_ENGINE": "0"})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "llama.cpp is not installed, so no model can run yet" in completed.stdout + completed.stderr


@pytest.mark.parametrize("manager,toolkit", [
    ("pacman", "cuda"), ("apt-get", "nvidia-cuda-toolkit"),
    ("dnf", "cuda-toolkit"), ("zypper", "cuda-toolkit"),
])
def test_source_build_installs_missing_cuda_toolkit(engine_installer, supply_build_packages, manager, toolkit):
    run, prefix, commands, calls = engine_installer
    supply_build_packages(manager)
    # Reproduce uv's PATH setup script shadowing env, including apt's package command.
    (commands / "env").unlink()
    (commands / "env").write_text("#!/bin/bash\nexit 0\n")
    (commands / "env").chmod(0o755)

    built = run("--from-source", "--backend", "cuda-13.4", "--yes")
    assert built.returncode == 0, built.stdout + built.stderr
    logged = calls.read_text()
    assert toolkit in next(line for line in logged.splitlines() if line.startswith("packages ") and "update" not in line)
    assert f"-DCMAKE_CUDA_COMPILER={commands}/nvcc" in logged
    assert "-DGGML_CUDA=ON" in logged and "cmake --build" in logged
    assert logged.index("packages ") < logged.index("git clone") < logged.index("cmake --build")
    assert (prefix / "current/llama-server").is_file()


def test_source_build_finds_new_cuda_toolkit_without_a_new_login(engine_installer, supply_build_packages, tmp_path):
    run, prefix, commands, calls = engine_installer
    supply_build_packages()
    cuda_root = tmp_path / "system/cuda"
    built = run("--from-source", "--backend", "cuda-13.4", env={"TEST_CUDA_ROOT": str(cuda_root)})
    assert built.returncode == 0, built.stdout + built.stderr
    assert not (commands / "nvcc").exists()
    assert f"-DCMAKE_CUDA_COMPILER={cuda_root}/bin/nvcc" in calls.read_text()


def test_source_build_reuses_existing_cuda_toolkit_outside_path(engine_installer, tmp_path):
    run, prefix, commands, calls = engine_installer
    cuda_root = tmp_path / "custom CUDA"
    (cuda_root / "bin").mkdir(parents=True)
    (cuda_root / "bin/nvcc").write_text("#!/bin/bash\nexit 0\n")
    (cuda_root / "bin/nvcc").chmod(0o755)
    (cuda_root / "include").mkdir()
    (cuda_root / "lib64").mkdir()
    for name in ("include/cuda_runtime.h", "include/cublas_v2.h", "lib64/libcudart.so", "lib64/libcublas.so"):
        (cuda_root / name).touch()
    built = run("--from-source", "--backend", "cuda-13.4", env={"CUDA_PATH": str(cuda_root)})
    assert built.returncode == 0, built.stdout + built.stderr
    assert f"-DCMAKE_CUDA_COMPILER={cuda_root}/bin/nvcc" in calls.read_text()
    assert "packages " not in calls.read_text()


def test_source_build_installs_missing_tools_and_development_headers(engine_installer, supply_build_packages, tmp_path):
    run, prefix, commands, calls = engine_installer
    supply_build_packages("apt-get")
    for tool in ("git", "cmake", "cc", "c++", "make", "pkg-config"):
        (commands / tool).unlink()
    (tmp_path / "missing-requirements/openssl").touch()
    built = run("--from-source")
    assert built.returncode == 0, built.stdout + built.stderr
    packages = next(line for line in calls.read_text().splitlines() if line.startswith("packages ") and "update" not in line)
    for required in ("git", "cmake", "build-essential", "pkg-config", "libssl-dev"):
        assert required in packages
    assert "packages apt-get update" in calls.read_text()
    assert "cuda" not in packages and "vulkan" not in packages and "rocm" not in packages
    assert (prefix / "current/llama-server").is_file()


def test_vulkan_build_installs_shader_compiler_and_headers(engine_installer, supply_build_packages, tmp_path):
    run, prefix, commands, calls = engine_installer
    supplied = supply_build_packages()
    shutil.copy2(supplied / "glslc", commands / "glslc")
    (tmp_path / "missing-requirements/SPIRV-Headers").touch()
    built = run("--from-source", "--backend", "vulkan")
    assert built.returncode == 0, built.stdout + built.stderr
    for required in ("shaderc", "spirv-headers", "vulkan-headers", "vulkan-icd-loader"):
        assert required in calls.read_text()
    assert "-DGGML_VULKAN=ON" in calls.read_text()


def test_rocm_build_installs_hip_and_blas_libraries(engine_installer, supply_build_packages, tmp_path):
    run, prefix, commands, calls = engine_installer
    supply_build_packages()
    rocm_root = tmp_path / "system/rocm"
    built = run("--from-source", "--backend", "rocm", env={"ROCM_PATH": str(rocm_root)})
    assert built.returncode == 0, built.stdout + built.stderr
    for required in ("hip-runtime-amd", "hipblas", "rocblas"):
        assert required in calls.read_text()
    assert "-DGGML_HIP=ON" in calls.read_text()
    assert f"-DCMAKE_PREFIX_PATH={rocm_root}" in calls.read_text()


@pytest.mark.parametrize("failure,reason", [
    ({"FAIL_PACKAGES": "1"}, "Could not install llama.cpp's build requirements"),
    ({"SKIP_SUPPLY": "1"}, "still needs: cuda"),
])
def test_missing_build_requirements_stop_before_compiling(engine_installer, supply_build_packages, failure, reason):
    run, prefix, commands, calls = engine_installer
    supply_build_packages()
    built = run("--from-source", "--backend", "cuda-13.4", env=failure)
    assert built.returncode != 0
    assert reason in built.stdout + built.stderr
    assert "git clone" not in calls.read_text() and "cmake" not in calls.read_text()
    assert not (prefix / "current").exists()


def test_missing_build_requirements_without_a_package_manager_stop_clearly(engine_installer):
    run, prefix, commands, calls = engine_installer
    built = run("--from-source", "--backend", "cuda-13.4")
    assert built.returncode != 0
    assert "no supported package manager was found" in built.stdout + built.stderr
    assert not calls.exists() and not (prefix / "current").exists()


def test_cuda_build_installs_development_libraries_when_nvcc_is_already_present(engine_installer, supply_build_packages):
    run, prefix, commands, calls = engine_installer
    supplied = supply_build_packages()
    shutil.copy2(supplied / "nvcc", commands / "nvcc")
    built = run("--from-source", "--backend", "cuda-13.4")
    assert built.returncode == 0, built.stdout + built.stderr
    assert "packages pacman -S --needed --noconfirm cuda" in calls.read_text()
    assert (prefix / "current/llama-server").is_file()


@pytest.mark.parametrize("system", ["0", "1"])
def test_main_source_install_supplies_its_cuda_requirements(engine_installer, supply_build_packages, system):
    run, prefix, commands, calls = engine_installer
    supply_build_packages()
    built = run(main_mode="source", env={"TEST_SYSTEM": system, "TEST_BACKEND": "cuda-13.4"})
    assert built.returncode == 0, built.stdout + built.stderr
    assert "packages pacman -S --needed --noconfirm cuda" in calls.read_text()
    assert "cmake --build" in calls.read_text() and "curl" not in calls.read_text()
    assert (prefix / "current").resolve() == prefix / "releases/b12008-cuda-13.4-source"


@pytest.mark.parametrize("options", [("--prebuilt",), ("--check",)])
def test_prebuilt_and_check_do_not_install_build_requirements(engine_installer, supply_build_packages, options):
    run, prefix, commands, calls = engine_installer
    supply_build_packages()
    for tool in ("cmake", "cc", "c++", "make", "pkg-config"):
        (commands / tool).unlink()
    checked = run(*options)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert not calls.exists() or "packages " not in calls.read_text()
