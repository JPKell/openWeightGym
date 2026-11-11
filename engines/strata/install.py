#!/usr/bin/env python3
"""Install the reviewed CUDA Strata runtime without downloading or loading models."""
import argparse
from dataclasses import asdict
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engines.strata.config import NodeSettings
from engines.strata.runtime import REVISION, verify_source

REPOSITORY = "https://github.com/Niko1221/Strata.git"
# Only the dependencies used by the controlled text bridge, from the reviewed requirements.
REQUIREMENTS = ("numpy==2.5.3", "jinja2==3.1.6", "regex==2026.9.10", "markupsafe==3.0.3")


def run(command, **kwargs):
    """Keep build commands as argument vectors."""
    return subprocess.run([str(value) for value in command], check=True, **kwargs)


def digest(path):
    """Hash an installed native artifact."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path, text, mode=0o600):
    """Publish a complete file beside its previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            os.chmod(temporary, mode)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def configure(root, node):
    """Change only the Strata table, keeping a private copy of previous settings."""
    path = root / "settings/loadcoach.toml"
    text = path.read_text()
    options = asdict(node)
    if tomllib.loads(text).get("engines", {}).get("strata") == options:
        return
    block = "[engines.strata]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in options.items())
    pattern = r"(?ms)^\[engines\.strata\][ \t]*(?:#[^\n]*)?\n.*?(?=^\[|\Z)"
    made = re.sub(pattern, lambda _: block + "\n", text) if re.search(pattern, text) else text.rstrip() + "\n\n" + block
    if tomllib.loads(made)["engines"]["strata"] != options:
        raise ValueError("Could not safely configure engines.strata")
    backup = root / "data/installation/strata-backups"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    atomic_write(backup / (path.name + ".before-strata-" + stamp), text)
    atomic_write(path, made)


def node_settings(target, budget):
    """Use stable paths: moving a Python environment breaks its interpreter paths."""
    return NodeSettings.read({"enabled": True, "allow_host_ram": True, "host_ram_mib": budget,
        "source": str(target / "source"), "python": str(target / "venv/bin/python"),
        "executable": str(target / "build/strata")})


def verify(node, *, recorded=True):
    """Check the source, native libraries and isolated Python imports without a model."""
    node.check_paths()
    verify_source(Path(node.source))
    native = Path(node.executable)
    hashes = {path.name: digest(path) for path in (native, native.with_name("libstrata_kq.so"))}
    if recorded:
        manifest = json.loads((native.parent.parent / "installation.json").read_text())
        if manifest["revision"] != REVISION or manifest["executables"] != hashes:
            raise ValueError("Installed Strata artifacts changed; keep this release and install into a new --prefix")
    for path in (native, native.with_name("libstrata_kq.so")):
        linked = run(["ldd", path], capture_output=True, text=True)
        if "not found" in linked.stdout:
            raise ValueError("Strata shared libraries are missing: " + linked.stdout)
    run([node.python, "-I", "-B", "-c",
         "import sys; sys.path[:0] = [sys.argv[1], sys.argv[1] + '/tools']; "
         "import numpy, gguf_reader, strata_tokenizer, serve.server", node.source], timeout=30)
    return hashes


def install(args):
    """Build outside the clean source tree and publish settings only after verification."""
    root = args.root.expanduser().resolve()
    prefix = (args.prefix or root / "runtimes/strata").expanduser().resolve()
    target = prefix / "releases" / (REVISION + "-cuda")
    node = node_settings(target, args.host_ram_mib)
    if args.check:
        options = tomllib.loads((root / "settings/loadcoach.toml").read_text()).get("engines", {}).get("strata", {})
        if not options.get("enabled"):
            print("Strata: not installed (optional); run owg install --strata install")
            return
        verify(NodeSettings.read(options))
        print("Strata source, native libraries and Python environment verified.")
        return
    print(f"Strata: CUDA; revision {REVISION}; host RAM budget {args.host_ram_mib} MiB; target {target}")
    if args.dry_run:
        print("Would build the reviewed native runtime, create private Python, configure Strata and delegate worker memory. No models downloaded.")
        return
    if not (root / "settings/loadcoach.toml").is_file():
        raise ValueError("Install loadcoach first, then run owg install --strata install")
    manifest = target / "installation.json"
    if not manifest.exists():
        missing = [tool for tool in ("git", "cmake", "c++", "nvcc") if not shutil.which(tool)]
        if missing:
            raise ValueError("Missing build tools: " + ", ".join(missing) + "; install the CUDA toolkit and C++ build tools, then retry")
        target.mkdir(parents=True, exist_ok=True)
        source = Path(node.source)
        if not source.exists():
            run(["git", "clone", "--no-checkout", REPOSITORY, source])
        # A partial fetch can be retried without resetting any modified checkout.
        if not (source / "serve/server.py").exists():
            run(["git", "-C", source, "checkout", "--detach", REVISION])
        verify_source(source)
        build = target / "build"
        run(["cmake", "-S", source, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
             "-DSTRATA_ENABLE_CUDA=ON", "-DSTRATA_BUILD_TESTS=OFF", "-DSTRATA_NATIVE_EXPERTS=ON",
             "-DCMAKE_CUDA_ARCHITECTURES=native"])
        run(["cmake", "--build", build, "--target", "strata", "strata_kq", "--parallel", args.jobs])
        run([sys.executable, "-m", "venv", target / "venv"])
        run([node.python, "-m", "pip", "install", "--disable-pip-version-check", *REQUIREMENTS])
        hashes = verify(node, recorded=False)
        atomic_write(manifest, json.dumps({"revision": REVISION, "executables": hashes}, indent=2) + "\n")
    else:
        verify(node)
    # Preserve all existing ceilings; the adapter refuses a budget above MemoryMax.
    unit_dir = args.unit_dir or Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
    dropin = unit_dir / "loadcoach.service.d/50-owg-strata.conf"
    delegation = (root / "services/loadcoach-strata.conf").read_text()
    if dropin.exists() and dropin.read_text() != delegation:
        raise ValueError(f"Keep your existing {dropin}; reconcile its delegation settings before retrying")
    atomic_write(dropin, delegation, 0o644)
    configure(root, node)
    print("Strata installed. Restart loadcoach to activate memory delegation; its existing memory ceilings remain in force. Models and tokenizers are installed separately.")


def main():
    """Parse explicit installation choices."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--unit-dir", type=Path)
    parser.add_argument("--host-ram-mib", type=int, default=22528)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 32 or not 1 <= args.host_ram_mib <= 2**31:
        parser.error("Use 1–32 build jobs and a positive host RAM budget up to 2147483648 MiB")
    try:
        install(args)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as problem:
        raise SystemExit("Strata installation failed: " + str(problem)) from None


if __name__ == "__main__":
    main()
