#!/usr/bin/env python3
"""Plan `owg upgrade` for the model engines installed here, and set releases aside for reinstalls.

    engine-upgrades.py plan --root ROOT [--llama-build TAG]
        One line per engine this installation runs, fields separated by \x1f (unit separator):
        engine, label, action, installed, target, backend, release folder, extra, note.
        action is upgrade (a newer release can be installed beside the current one), reinstall
        (no newer release can be installed, so the current one is removed and rebuilt), current
        (llama.cpp is already the newest release), unchecked (GitHub could not be asked) or
        unmanaged (not installed by openWeightGym).
    engine-upgrades.py set-aside --root ROOT ENGINE
        Move the release in use, with its build and source folders, to PREFIX/.reinstall-STAMP.
    engine-upgrades.py restore --root ROOT ENGINE    put a set-aside release back after a failure
    engine-upgrades.py discard --root ROOT ENGINE    delete it after a verified reinstall

llama.cpp moves to its newest release and stable-diffusion.cpp to the newest commit on its default
branch, which is built unpatched. The other engines build the revision this checkout was qualified
with, so `owg update` brings their newer versions.
"""
import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import platform
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ("llamacpp", "sdcpp", "audiocpp", "whispercpp", "strata")
LABELS = {"llamacpp": "llama.cpp", "sdcpp": "stable-diffusion.cpp", "audiocpp": "audio.cpp",
          "whispercpp": "whisper.cpp", "strata": "Strata"}
ALIASES = {"llama": "llamacpp", "llama.cpp": "llamacpp", "llamacpp": "llamacpp",
           "sdcpp": "sdcpp", "sd.cpp": "sdcpp", "stable-diffusion.cpp": "sdcpp", "stablediffusion.cpp": "sdcpp",
           "audiocpp": "audiocpp", "audio.cpp": "audiocpp",
           "whispercpp": "whispercpp", "whisper.cpp": "whispercpp", "whisper": "whispercpp",
           "strata": "strata"}
LLAMA_RELEASE = re.compile(r"(b[0-9]+|v[0-9]+\.[0-9]+\.[0-9]+)-(cuda-13\.[45]|cuda-12\.8|rocm|vulkan|cpu)(-source)?")
LLAMA_LATEST = "https://github.com/ggml-org/llama.cpp/releases/latest"
LLAMA_DOWNLOADS = "https://github.com/ggml-org/llama.cpp/releases/download"
SET_ASIDE = ".reinstall-"


def settings(root):
    """The model runner's settings, or nothing on a machine without one."""
    try:
        return tomllib.loads((root / "settings/loadcoach.toml").read_text())
    except FileNotFoundError:
        return {}


def pinned(engine):
    """The revision this checkout installs for an engine that builds a single qualified revision."""
    sys.path.insert(0, str(ROOT))
    if engine == "audiocpp":
        from soundroom.install import REVISION
    elif engine == "whispercpp":
        from engines.whisper_cpp.install import REVISION
    else:
        from engines.strata.runtime import REVISION
    return REVISION


def latest_sdcpp():
    """The commit stable-diffusion.cpp's default branch points at now; it is built unpinned."""
    sys.path.insert(0, str(ROOT))
    from mirrorwall.install import latest_revision
    return latest_revision()


def latest_llama():
    """The newest ggml-org release tag, from where GitHub's latest-release page redirects."""
    request = urllib.request.Request(LLAMA_LATEST, method="HEAD", headers={"User-Agent": "openWeightGym"})
    with urllib.request.urlopen(request, timeout=20) as answer:
        tag = answer.geturl().rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"b[0-9]+|v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError(f"GitHub named an unexpected latest release: {tag}")
    return tag


def llama_asset(tag, backend):
    """The prebuilt download install-llamacpp.sh would fetch for a backend, or None when there is none."""
    arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    build = tag
    if tag.startswith("v"):  # version releases publish their binaries under a nightly they name
        with urllib.request.urlopen(f"{LLAMA_DOWNLOADS}/{tag}/nightly-tag.txt", timeout=20) as answer:
            build = answer.read().decode().strip()
        if not re.fullmatch(r"b[0-9]+", build):
            raise ValueError(f"llama.cpp {tag} names an unexpected nightly: {build}")
    names = {"cpu": f"llama-{build}-bin-ubuntu-{arch}.tar.gz",
             "vulkan": f"llama-{build}-bin-ubuntu-vulkan-{arch}.tar.gz",
             "cuda-13.5": f"llama-{build}-bin-ubuntu-cuda-13.5-{arch}.tar.gz",
             "cuda-13.4": f"llama-{build}-bin-ubuntu-cuda-13.4-{arch}.tar.gz",
             "cuda-12.8": f"llama-{build}-bin-ubuntu-cuda-12.8-x64.tar.gz" if arch == "x64" else None,
             "rocm": f"llama-{build}-bin-ubuntu-rocm-10.0-x64.tar.gz" if arch == "x64" else None}
    return f"{LLAMA_DOWNLOADS}/{build}/{names[backend]}" if arch and names.get(backend) else None


def published(url):
    """Whether GitHub serves a download, without fetching it."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "openWeightGym"})
    try:
        with urllib.request.urlopen(request, timeout=20):
            return True
    except urllib.error.HTTPError as problem:
        if problem.code == 404:
            return False
        raise


def llama_backend(tag, backend):
    """The installed backend if the release offers it, otherwise the closest CUDA build that runs on
    the same driver: CUDA builds carry their own runtime, so an older one only needs an older driver."""
    choices = {"cuda-13.5": ("cuda-13.5", "cuda-13.4", "cuda-12.8"),
               "cuda-13.4": ("cuda-13.4", "cuda-12.8")}.get(backend, (backend,))
    for choice in choices:
        url = llama_asset(tag, choice)
        if url and published(url):
            return choice
    return None


def newer(tag, than):
    """Whether one llama.cpp tag is a later release; nightly numbers and versions sort separately."""
    def key(value):
        """The numbers in a tag, compared in order."""
        return tuple(map(int, re.findall(r"[0-9]+", value)))
    if tag[0] != than[0]:
        return tag != than
    return key(tag) > key(than)


def release(root, engine, config):
    """(release folder, installation manifest or None) of the configured runtime, or None."""
    if engine == "llamacpp":
        server = config.get("llama_server")
        return (Path(server).expanduser().resolve().parent, None) if server else None
    table = config.get("engines", {}).get(engine) or {}
    if not table.get("enabled"):
        return None
    if engine == "strata":
        folder = Path(table["executable"]).expanduser().parent.parent
    else:
        folder = Path(table["manifest"]).expanduser().parent
    try:
        manifest = json.loads((folder / "installation.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    return folder.resolve(), manifest


def plan_engine(root, engine, config, llama_build=None):
    """The planned action for one engine, or None when it is not installed here."""
    found = release(root, engine, config)
    if found is None:
        return None
    folder, manifest = found
    row = {"engine": engine, "label": LABELS[engine], "action": "reinstall", "installed": "", "target": "",
           "backend": "", "folder": str(folder), "extra": "", "note": ""}
    if engine == "llamacpp":
        match = LLAMA_RELEASE.fullmatch(folder.name)
        if folder.parent.name != "releases" or match is None:
            row.update(action="unmanaged", installed=str(folder),
                       note="llama_server is your own build; upgrade it yourself, or run owg install --llama auto")
            return row
        build, backend, source = match.groups()
        row.update(installed=build, backend=backend, extra="source" if source else "prebuilt")
        try:
            target = llama_build or latest_llama()
        except (OSError, ValueError) as problem:
            row.update(action="unchecked", note=f"could not read the newest release: {problem}")
            return row
        row["target"] = target
        if target == build:
            row.update(action="current", note=("the release asked for" if llama_build else "already the newest release") + "; owg upgrade --reinstall rebuilds it")
        elif llama_build or newer(target, build):
            row["action"] = "upgrade"
            if not source:
                try:
                    choice = llama_backend(target, backend)
                except (OSError, ValueError) as problem:
                    row.update(action="unchecked", note=f"could not check the {target} downloads: {problem}")
                    return row
                if choice is None:
                    row.update(action="unavailable",
                               note=f"{target} has no prebuilt {backend} download; owg upgrade --llama-build TAG picks another release")
                elif choice != backend:
                    row.update(backend=choice, note=f"{target} has no {backend} download; using its {choice} build, which runs on the same driver")
        else:
            row.update(action="current", note=f"{build} is newer than GitHub's latest release, {target}")
        return row
    revision = manifest.get("revision", "")
    row.update(installed=revision[:12] or "unknown", backend=manifest.get("backend", "cuda"))
    if engine == "sdcpp":
        try:
            target = latest_sdcpp()
        except (OSError, ValueError, subprocess.SubprocessError) as problem:
            row.update(action="unchecked", note=f"could not read the newest commit: {problem}")
            return row
        row["target"] = target  # the full commit, which the installer is asked to build
        if revision == target:
            row.update(action="current", note="already the newest upstream commit; owg upgrade --reinstall rebuilds it")
        else:
            row.update(action="upgrade", note="the newest upstream commit is built beside the current release, which is kept for going back")
        return row
    target = pinned(engine)
    row["target"] = target[:12]
    if engine == "strata":
        row["extra"] = str(config["engines"]["strata"].get("host_ram_mib", 22528))
    if revision != target:
        row["action"] = "upgrade"
        row["note"] = "this checkout qualifies a newer revision; the current release is kept for going back"
    else:
        row["note"] = "no newer qualified revision; owg update brings new ones"
    return row


def plan(root, llama_build=None):
    """Every installed engine's planned action, in the order they are upgraded."""
    config = settings(root)
    rows = [plan_engine(root, engine, config, llama_build) for engine in ENGINES]
    return [row for row in rows if row]


def folders(root, engine):
    """The release in use and the build and source folders it was made from."""
    found = release(root, engine, settings(root))
    if found is None:
        raise ValueError(f"{LABELS[engine]} is not installed here")
    folder, manifest = found
    if folder.parent.name != "releases":
        raise ValueError(f"{folder} is not a release openWeightGym installed")
    prefix = folder.parent.parent
    paths = [folder, prefix / "build" / folder.name]
    if engine == "llamacpp":
        paths.append(prefix / "source" / folder.name.split("-", 1)[0])
    elif engine != "strata":  # Strata keeps its source and build inside the release.
        revision = (manifest or {}).get("revision")
        if revision:
            paths += [path for path in (prefix / "source").glob(revision + "*") if path.is_dir()]
    return prefix, [path for path in paths if path.exists() or path.is_symlink()]


def set_aside(root, engine):
    """Move the release and its build inputs aside, recording where each one came from."""
    prefix, paths = folders(root, engine)
    if any(prefix.glob(SET_ASIDE + "*")):
        raise ValueError(f"An earlier reinstall left {prefix}/{SET_ASIDE}*: run owg upgrade again after "
                         "restoring or removing it")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside = prefix / (SET_ASIDE + stamp)
    aside.mkdir(mode=0o700)
    moved = []
    try:
        for index, path in enumerate(paths):
            destination = aside / str(index)
            path.rename(destination)
            moved.append({"from": str(path), "to": str(destination)})
    except OSError:
        for item in reversed(moved):
            Path(item["to"]).rename(item["from"])
        aside.rmdir()
        raise
    (aside / "moved.json").write_text(json.dumps(moved, indent=2) + "\n")
    print(aside)


def recorded(root, engine):
    """The set-aside folder for an engine and the moves it records."""
    found = release(root, engine, settings(root))
    if found is None:
        raise ValueError(f"{LABELS[engine]} is not installed here")
    prefix = found[0].parent.parent  # still named by settings while the release itself is aside
    candidates = sorted(prefix.glob(SET_ASIDE + "*"))
    if len(candidates) != 1:
        raise ValueError(f"Expected one set-aside {LABELS[engine]} release in {prefix}, found {len(candidates)}")
    return candidates[0], json.loads((candidates[0] / "moved.json").read_text())


def restore(root, engine):
    """Put a set-aside release back exactly where it was, replacing anything a failed build left."""
    aside, moved = recorded(root, engine)
    for item in moved:
        origin = Path(item["from"])
        if origin.is_symlink() or origin.is_file():
            origin.unlink()
        elif origin.exists():
            shutil.rmtree(origin)
        origin.parent.mkdir(parents=True, exist_ok=True)
        Path(item["to"]).rename(origin)
    (aside / "moved.json").unlink()
    aside.rmdir()
    print(f"Restored the previous {LABELS[engine]} release")


def discard(root, engine):
    """Delete a set-aside release once its replacement is verified."""
    aside, _ = recorded(root, engine)
    shutil.rmtree(aside)
    print(f"Removed the previous {LABELS[engine]} release")


def engine_name(value):
    """An engine's canonical name from any of the names people use for it."""
    name = ALIASES.get(value.lower())
    if name is None:
        raise argparse.ArgumentTypeError(f"{value} is not an engine; choose from {', '.join(LABELS.values())}")
    return name


def main():
    """Run one helper command for owg upgrade."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "set-aside", "restore", "discard", "name"))
    parser.add_argument("engine", nargs="?", type=engine_name)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--llama-build")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.llama_build and not re.fullmatch(r"b[0-9]+|v[0-9]+\.[0-9]+\.[0-9]+", args.llama_build):
        parser.error("A llama.cpp release looks like b12359 or v1.0.0")
    if args.command == "plan":
        for row in plan(root, args.llama_build):
            print("\x1f".join(row[key].replace("\x1f", " ").replace("\n", " ") for key in
                            ("engine", "label", "action", "installed", "target", "backend", "folder", "extra", "note")))
        return
    if args.engine is None:
        parser.error(f"{args.command} needs an engine")
    if args.command == "name":
        print(args.engine)
    elif args.command == "set-aside":
        set_aside(root, args.engine)
    elif args.command == "restore":
        restore(root, args.engine)
    else:
        discard(root, args.engine)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError) as problem:
        raise SystemExit(f"engine-upgrades: {problem}") from None
