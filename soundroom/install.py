#!/usr/bin/env python3
"""Build or adopt a pinned, optional audio.cpp runtime without restarting openWeightGym.

Only the engine's TOML table and its private installation directory are changed.
Verified releases are immutable; existing processes keep their original binary.
"""
import argparse
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REVISION = "0097d365a00b52a6991ad7618f0a04e8d2935e47"  # v0.9.2
REPOSITORY = "https://github.com/0xShug0/audio.cpp.git"
PROGRAMS = ("audiocpp_server", "audiocpp_cli")


def digest(path):
    """Hash a finished file before publishing it."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, **kwargs):
    """Run a fixed argument vector, never through a shell."""
    return subprocess.run([str(v) for v in command], check=True, **kwargs)


def configure(path, server, cli, manifest, backup_directory=None):
    """Replace only the engine table, preserving unrelated configuration bytes."""
    text = path.read_text() if path.exists() else ""
    options = {"enabled": True, "executable": str(server), "cli": str(cli), "manifest": str(manifest)}
    if tomllib.loads(text).get("engines", {}).get("audiocpp") == options:
        return
    block = "[engines.audiocpp]\n" + "".join(f"{k} = {str(v).lower() if type(v) is bool else json.dumps(v)}\n" for k, v in options.items())
    pattern = r"(?ms)^\[engines\.audiocpp\][ \t]*\n.*?(?=^\[|\Z)"
    made = re.sub(pattern, block + "\n", text) if re.search(pattern, text) else text.rstrip() + "\n\n" + block
    if tomllib.loads(made)["engines"]["audiocpp"] != options:
        raise ValueError("Could not safely configure engines.audiocpp")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_directory = backup_directory or path.parent / "audiocpp-backups"
        backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_directory / (path.name + ".before-audiocpp-" + stamp)
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    temporary = path.with_name(path.name + ".audiocpp-tmp")
    with temporary.open("x") as stream:
        os.chmod(temporary, 0o600)
        stream.write(made)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify(folder):
    """Verify installed hashes and the server options openWeightGym relies on, without loading a model."""
    recorded = json.loads((folder / "installation.json").read_text())
    for name, checksum in recorded["executables"].items():
        path = folder / "bin" / name
        if not path.is_file() or not os.access(path, os.X_OK) or digest(path) != checksum:
            raise ValueError("audio.cpp executable verification failed: " + name)
    if not (folder / "model_specs").is_dir():
        raise ValueError("audio.cpp release has no model_specs catalogue")
    answer = run([folder / "bin" / "audiocpp_server", "--help"], capture_output=True, text=True, timeout=30)
    help_text = answer.stdout + answer.stderr
    for flag in ("--config", "--host", "--port", "--backend", "--device", "--max-loaded-models"):
        if flag not in help_text:
            raise ValueError("audio.cpp build does not expose required flag " + flag)
    return recorded


def configured(root):
    """The release folder settings/loadcoach.toml runs audio.cpp from, or None when it runs none."""
    try:
        table = tomllib.loads((root / "settings/loadcoach.toml").read_text()).get("engines", {}).get("audiocpp") or {}
    except FileNotFoundError:
        return None
    return Path(table["manifest"]).expanduser().parent if table.get("enabled") and table.get("manifest") else None


def main():
    """Install a verified release and register it for subsequent engine launches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--prefix", type=Path, help="where releases are kept (default: ROOT/runtimes/audiocpp)")
    parser.add_argument("--backend", choices=("cuda", "vulkan", "cpu"), default="cuda")
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--source", type=Path, help="Existing clean source checkout; otherwise fetch pinned upstream")
    parser.add_argument("--adopt-build", type=Path, help="Use an existing build directory of the selected revision")
    parser.add_argument("--cuda-architectures", default="native")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    chosen = args.prefix is not None
    args.prefix = (args.prefix or args.root / "runtimes/audiocpp").expanduser().resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or not 1 <= args.jobs <= 32:
        parser.error("Use a full commit SHA and 1–32 build jobs")
    if not re.fullmatch(r"native|[0-9]+a?(?:-real|-virtual)?(?:;[0-9]+a?(?:-real|-virtual)?)*", args.cuda_architectures):
        parser.error("Invalid CUDA architectures")
    current = args.prefix / "current"
    if args.check:
        # The release this installation runs, not whatever an older one left in a shared folder.
        folder = current if chosen else configured(args.root)
        if folder is None or chosen and not folder.is_dir():
            print("audio.cpp: not installed (optional); run scripts/install-audiocpp.sh")
            return
        try:
            print(json.dumps(verify(folder), indent=2))
        except (OSError, KeyError, ValueError, subprocess.SubprocessError) as problem:
            raise SystemExit(f"audio.cpp in {folder} does not work: {problem}") from None
        return
    target = args.prefix / "releases" / (args.revision + "-" + args.backend)
    print(f"audio.cpp: {args.backend}; revision {args.revision}; target {target}")
    if args.dry_run:
        print("Would verify/build audiocpp_server and audiocpp_cli, atomically select the release, and configure settings/loadcoach.toml. Model weights are separate.")
        return
    if not target.exists():
        if not args.adopt_build:
            from engines.build import build_environment
            environment = build_environment(args.backend, "audio.cpp")
        args.prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.mkdir(exist_ok=True)
        source = args.source.expanduser().resolve() if args.source else args.prefix / "source" / args.revision
        if not source.exists():
            if args.source:
                raise ValueError("Source checkout does not exist")
            source.parent.mkdir(parents=True, exist_ok=True)
            # Its one submodule holds optional server frontends, which openWeightGym does not build.
            run(["git", "clone", REPOSITORY, source])
            run(["git", "-C", source, "checkout", "--detach", args.revision])
        revision = run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        if revision != args.revision:
            raise ValueError("Source checkout is not at the selected pinned revision")
        run(["git", "-C", source, "diff", "--quiet", "HEAD", "--"])
        build = args.adopt_build.expanduser().resolve() if args.adopt_build else args.prefix / "build" / (args.revision + "-" + args.backend)
        flags = ["-DCMAKE_BUILD_TYPE=Release", "-DAUDIOCPP_MODEL_SET=full", "-DAUDIOCPP_BUILD_SERVER_FRONTENDS=OFF",
                 "-DAUDIOCPP_BUILD_NATIVE_MODEL_MANAGER=OFF"]
        if args.backend == "cuda":
            flags += ["-DENGINE_ENABLE_CUDA=ON", "-DCMAKE_CUDA_ARCHITECTURES=" + args.cuda_architectures]
        elif args.backend == "vulkan":
            flags += ["-DENGINE_ENABLE_VULKAN=ON"]
        if not args.adopt_build:
            if args.backend == "cuda":
                flags.append("-DCMAKE_CUDA_COMPILER=" + environment["CUDACXX"])
            run(["cmake", "-S", source, "-B", build, *flags], env=environment)
            run(["cmake", "--build", build, "--config", "Release", "--target", *PROGRAMS, "--parallel", args.jobs], env=environment)
        cache = build / "CMakeCache.txt"
        if not cache.is_file():
            raise ValueError("Build has no CMakeCache.txt provenance")
        cache_text = cache.read_text()
        if args.backend == "cuda" and "ENGINE_ENABLE_CUDA:BOOL=ON" not in cache_text:
            raise ValueError("Selected build does not have the CUDA backend")
        if f"CMAKE_HOME_DIRECTORY:INTERNAL={source}" not in cache_text:
            raise ValueError("Selected build was not made from the pinned source checkout")
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=target.parent) as temporary:
            stage = Path(temporary)
            (stage / "bin").mkdir()
            for name in PROGRAMS:
                shutil.copy2(build / "bin" / name, stage / "bin" / name)
            shutil.copytree(source / "model_specs", stage / "model_specs")
            manifest = {"version": 1, "repository": REPOSITORY, "revision": revision, "backend": args.backend,
                        "build_flags": flags, "build_cache_sha256": digest(cache),
                        "executables": {name: digest(stage / "bin" / name) for name in PROGRAMS},
                        "devices_note": "One device per model; component placement across cards is unqualified"}
            (stage / "installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
            verify(stage)
            stage.rename(target)
    verify(target)
    link = args.prefix / ".current-new"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    link.replace(current)
    configure(args.root / "settings/loadcoach.toml", target / "bin/audiocpp_server", target / "bin/audiocpp_cli",
              target / "installation.json", args.root / "data/installation/audiocpp-backups")
    print("audio.cpp installed and configured for new launches. No service was restarted and no model was downloaded.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as problem:
        raise SystemExit("audio.cpp installation failed: " + str(problem)) from None
