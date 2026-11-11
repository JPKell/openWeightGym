"""Native builders share persistent toolkit installation, including service-account builds."""
import os
import subprocess

import pytest

from engines import build
from engines.strata import install as strata
from test_install_components import strata_install  # noqa: F401
from test_llamacpp_setup import engine_installer, supply_build_packages  # noqa: F401
from test_setup_recovery import script_functions, shell

REAL_BUILD_ENVIRONMENT = build.build_environment


@pytest.mark.parametrize("label", ["Strata", "stable-diffusion.cpp", "audio.cpp"])
@pytest.mark.parametrize("manager,package", [("apt-get", "nvidia-cuda-toolkit"), ("pacman", "cuda"),
                                            ("dnf", "cuda-toolkit"), ("zypper", "cuda-toolkit")])
def test_optional_builders_install_cuda_once_and_keep_it(engine_installer, supply_build_packages, label, manager, package):
    run, _, commands, calls = engine_installer
    supply_build_packages(manager)
    first = run(toolchain=("cuda", label))
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout.strip() == str(commands / "nvcc")
    assert f"Installing {label} build requirements" in first.stderr
    assert package in calls.read_text()
    assert "git clone" not in calls.read_text() and "cmake --build" not in calls.read_text()
    before = calls.read_text()
    again = run(toolchain=("cuda", label))
    assert again.returncode == 0, again.stderr
    assert calls.read_text() == before and (commands / "nvcc").exists()


@pytest.mark.parametrize("backend,needed", [("vulkan", "shaderc"), ("cpu", "build-essential")])
def test_non_cuda_builds_only_gather_their_own_toolkit(engine_installer, supply_build_packages, backend, needed):
    run, _, commands, calls = engine_installer
    supply_build_packages("apt-get" if backend == "cpu" else "pacman")
    (commands / "cmake").unlink()
    result = run(toolchain=(backend, "audio.cpp"))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and needed in calls.read_text()
    packages = next(line.split() for line in calls.read_text().splitlines() if line.startswith("packages ") and "update" not in line)
    assert not {"cuda", "cuda-toolkit", "nvidia-cuda-toolkit"}.intersection(packages)


def prepared_environment(monkeypatch, engine_installer):
    """Use the fixture's real shell entry point with only fake package/build tools on PATH."""
    run, prefix, _, _ = engine_installer
    for key, value in run.environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OWG_NO_TTY", "1")
    monkeypatch.setattr(build, "__file__", str(prefix.parents[1] / "engines/build.py"))


@pytest.mark.parametrize("selector", ["CUDACXX", "CUDAToolkit_ROOT", "CUDA_PATH", "CUDA_HOME", "standard"])
def test_python_build_environment_finds_cuda_outside_path(engine_installer, tmp_path, monkeypatch, selector):
    _, _, commands, calls = engine_installer
    cuda = tmp_path / ("system/cuda" if selector == "standard" else "custom CUDA")
    for folder in ("bin", "include", "lib64"):
        (cuda / folder).mkdir(parents=True, exist_ok=True)
    compiler = cuda / "bin/nvcc"
    compiler.write_text("#!/bin/bash\nexit 0\n"); compiler.chmod(0o755)
    for name in ("include/cuda_runtime.h", "include/cublas_v2.h", "lib64/libcudart.so", "lib64/libcublas.so"):
        (cuda / name).touch()
    prepared_environment(monkeypatch, engine_installer)
    if selector != "standard":
        monkeypatch.setenv(selector, str(compiler if selector == "CUDACXX" else cuda))
    environment = build.build_environment("cuda", "Strata")
    assert environment["CUDACXX"] == str(compiler)
    assert environment["PATH"].split(os.pathsep)[0] == str(compiler.parent)
    assert not (commands / "nvcc").exists() and not calls.exists()


def test_strata_retry_reuses_installed_cuda_and_passes_compiler_to_cmake(
        engine_installer, supply_build_packages, strata_install, monkeypatch):
    args, config, build_calls, fail = strata_install
    run, _, commands, package_calls = engine_installer
    supply_build_packages()
    prepared_environment(monkeypatch, engine_installer)
    # The Strata fixture normally bypasses packages; exercise the actual shared boundary here.
    monkeypatch.setattr(build, "build_environment", REAL_BUILD_ENVIRONMENT)
    original = config.read_bytes()
    fail.append(True)
    with pytest.raises(subprocess.CalledProcessError):
        strata.install(args)
    assert config.read_bytes() == original and (commands / "nvcc").is_file()
    before = package_calls.read_text()
    fail.clear()
    strata.install(args)
    assert package_calls.read_text() == before
    assert any("-DCMAKE_CUDA_COMPILER=" + str(commands / "nvcc") in command for command in build_calls)
    assert "pip" not in before and "remove" not in before and "purge" not in before


def test_package_failure_stops_before_strata_downloads_or_changes_settings(
        engine_installer, supply_build_packages, strata_install, monkeypatch):
    args, config, build_calls, _ = strata_install
    supply_build_packages()
    monkeypatch.setattr(build, "build_environment", REAL_BUILD_ENVIRONMENT)
    prepared_environment(monkeypatch, engine_installer)
    monkeypatch.setenv("FAIL_PACKAGES", "1")
    original = config.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        strata.install(args)
    assert not build_calls and config.read_bytes() == original
    assert not (args.root / "runtimes").exists()


@pytest.mark.parametrize("engine", ["image", "audio"])
@pytest.mark.parametrize("failed_packages", [False, True])
def test_image_and_audio_builders_prepare_tools_before_git_and_use_selected_compiler(tmp_path, monkeypatch, engine, failed_packages):
    import sys
    from mirrorwall import install as image
    from soundroom import install as audio
    installer = image if engine == "image" else audio
    expected_label = "stable-diffusion.cpp" if engine == "image" else "audio.cpp"
    events = []
    environment = {"CUDACXX": "/custom CUDA/bin/nvcc", "PATH": "/custom CUDA/bin:/usr/bin"}

    def prepare(backend, label):
        events.append((backend, label))
        if failed_packages:
            raise subprocess.CalledProcessError(1, ["fixture-package-manager"])
        return environment

    def run(command, **kwargs):
        command = list(map(str, command))
        events.append(command)
        if command[0] == "cmake":
            assert kwargs["env"] == environment
            assert "-DCMAKE_CUDA_COMPILER=/custom CUDA/bin/nvcc" in command
            raise RuntimeError("reached compilation")
        return subprocess.CompletedProcess(command, 0, stdout=installer.REVISION if "rev-parse" in command else "", stderr="")

    source = tmp_path / "source"; source.mkdir()
    root = tmp_path / "suite"
    arguments = ["installer", "--root", str(root), "--source", str(source), "--backend", "cuda"]
    if engine == "image":
        arguments.append("--upstream-only")
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(build, "build_environment", prepare)
    monkeypatch.setattr(installer, "run", run)
    with pytest.raises(subprocess.CalledProcessError if failed_packages else RuntimeError):
        installer.main()
    assert events[0] == ("cuda", expected_label)
    if failed_packages:
        assert len(events) == 1 and not root.exists()


def test_system_install_prepares_packages_before_switching_owner(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    helper = scripts / "install-build-tools.sh"
    helper.write_text('#!/bin/bash\nprintf "tools:%s:%s\\n" "$1" "$2" >>"$EVENTS"\n')
    helper.chmod(0o755)
    answer = shell(tmp_path, script_functions("install.sh") + '''
source "$TEST_REPO/scripts/lib/ui.sh"
parse_arguments --yes --system --topology all --strata install --audiocpp install --audiocpp-backend cpu
ui_init
DIR=$INSTALL HOST_ADDRESS=127.0.0.1 UV=/fixture/uv
save_machine_key() { :; }
as_owner() { printf 'owner:%s\\n' "$1" >>"$EVENTS"; }
setting_up
''', env={"INSTALL": str(tmp_path), "EVENTS": str(tmp_path / "events")})
    assert answer.returncode == 0, answer.stderr
    assert (tmp_path / "events").read_text().splitlines() == [
        "tools:cuda:Strata", "tools:cpu:audio.cpp", f"owner:{tmp_path}/scripts/install-suite.sh"]
