"""Prepare persistent system build tools using the shared native-installer policy."""
import os
from pathlib import Path
import subprocess


def build_environment(backend: str, label: str) -> dict[str, str]:
    """Install missing tools and expose an existing or newly installed CUDA compiler to CMake."""
    helper = Path(__file__).resolve().parents[1] / "scripts/install-build-tools.sh"
    prepared = subprocess.run(["bash", str(helper), backend, label], check=True,
                              stdout=subprocess.PIPE, text=True)
    environment = dict(os.environ)
    compiler = prepared.stdout.strip()
    if backend == "cuda":
        if not compiler or not Path(compiler).is_file() or not os.access(compiler, os.X_OK):
            raise ValueError("CUDA build tools did not provide an executable nvcc")
        environment["CUDACXX"] = compiler
        environment["PATH"] = str(Path(compiler).parent) + os.pathsep + environment.get("PATH", os.defpath)
    return environment
