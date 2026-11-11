#!/usr/bin/env python3
"""Preview or discard a pending installer plan without uninstalling working components."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile


PROGRAMS = ("loadcoach", "toollocker", "spotter", "voice", "weightroom", "turnstile", "voice-gateway")


def children(path):
    """Inspect only real installer directories, never redirected runtime trees."""
    if path.resolve() != path or not path.is_dir():
        return []
    return sorted(child for child in path.iterdir() if child.is_dir() and not child.is_symlink())


def related(left, right):
    """Protect both a referenced file and any containing directory."""
    return left == right or left in right.parents or right in left.parents


def partial_builds(root, receipt):
    """Find unfinished builds of requested engines; published and configured releases survive."""
    config = root / "settings/loadcoach.toml"
    settings = {}
    if config.exists():
        try:
            import tomllib
        except ImportError:
            raise ValueError("Use Python 3.11 or newer to read existing engine settings safely") from None
        settings = tomllib.loads(config.read_text())
    protected = []
    if settings.get("llama_server"):
        value = Path(settings["llama_server"]).expanduser()
        protected.append((value if value.is_absolute() else root / value).resolve())
    for table in settings.get("engines", {}).values():
        if isinstance(table, dict):
            for key in ("executable", "cli", "manifest", "source", "python"):
                if table.get(key):
                    value = Path(table[key]).expanduser()
                    # Relative engine paths belong to this installation, never the caller's cwd.
                    protected.append((value if value.is_absolute() else root / value).resolve())
    for engine in ("strata", "sdcpp", "audiocpp", "llamacpp"):
        current = root / "runtimes" / engine / "current"
        if current.exists() or current.is_symlink():
            protected.append(current.resolve())
    remove = []
    for engine in ("strata", "sdcpp", "audiocpp", "llamacpp"):
        requested = receipt.get(engine) == "install"
        if engine == "llamacpp":
            choice = receipt.get("llama", "skip")
            requested = choice not in ("", "skip") and not choice.startswith("/")
        if not requested:
            continue
        prefix = root / "runtimes" / engine
        if prefix.resolve() != prefix:
            continue
        releases = children(prefix / "releases")
        completed = [path for path in releases if not path.name.startswith(".stage-") and
                     ((path / "installation.json").exists() or
                      (engine == "llamacpp" and (path / "llama-server").is_file()))]
        protected.extend(completed)
        revisions = []
        unknown_revision = False
        for release in completed:
            if engine == "llamacpp":
                revisions.append(release.name.split("-", 1)[0])
                continue
            try:
                revision = json.loads((release / "installation.json").read_text())["revision"]
                if not isinstance(revision, str) or not revision:
                    raise ValueError("Missing revision")
                revisions.append(revision)
            except (OSError, ValueError, KeyError, TypeError):
                # A legacy/edited manifest still protects its source from automatic removal.
                unknown_revision = True
        candidates = releases + children(prefix / "build")
        for source in children(prefix / "source"):
            if not unknown_revision and not any(source.name == rev or source.name.startswith(rev + "-")
                                                for rev in revisions):
                candidates.append(source)
        for candidate in candidates:
            if candidate.parent.name == "build" and any(candidate.name == path.name for path in completed):
                continue
            if not any(related(candidate, path) for path in protected):
                remove.append(candidate)
    return sorted(set(remove))


def installed_services(root, receipt):
    """Keep services with units belonging to this installation, including completed additions."""
    unit_dir = Path(receipt.get("unit_dir") or
                    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user")
    installed = []
    for service in PROGRAMS:
        unit = unit_dir / (service + ".service")
        if unit.is_file():
            text = unit.read_text().replace("%h", str(Path.home()))
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    command = shlex.split(line.partition("=")[2])
                    if command and command[0].startswith(str(root) + "/.venv/bin/"):
                        installed.append(service)
                        break
    return installed


def check_stopped(receipt):
    """Refuse cleanup of a still-running recorded installer, accounting for PID reuse."""
    pid, started = receipt.get("installer_pid", ""), receipt.get("installer_start", "")
    if not pid.isdecimal() or not started:
        return
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return
    fields = status.rsplit(")", 1)[1].split()
    if fields[19] == started and fields[0] != "Z":
        raise ValueError("An installation is still running; let it finish or stop it before discarding its files")


def discard(root, apply=False):
    """Clear the checkpoint only after cleanup succeeds; keep a private receipt backup."""
    root = root.expanduser().resolve()
    receipt_path = root / ".install-receipt"
    if not receipt_path.exists():
        receipt_path = root / "data/install-receipt"
    if not receipt_path.exists():
        print("No failed installation plan to discard.")
        return
    if receipt_path.is_symlink():
        raise ValueError("Refusing to replace a symlinked installation receipt")
    original = receipt_path.read_text()
    receipt = dict(line.split("=", 1) for line in original.splitlines()
                   if "=" in line and not line.startswith("#"))
    if receipt.get("state") != "pending":
        print("No failed installation plan to discard.")
        return
    check_stopped(receipt)
    paths = partial_builds(root, receipt)
    services = installed_services(root, receipt)
    print("Cancel all saved installation choices; completed components and shared files are kept.")
    for path in paths:
        print(f"Remove partial build: {path}")
    if not paths:
        print("No removable partial engine builds found.")
    if not apply:
        return
    if receipt_path.read_text() != original:
        raise ValueError("Installation choices changed during cleanup; preview the plan again")
    check_stopped(receipt)
    with tempfile.NamedTemporaryFile(mode="w", prefix=".install-receipt.before-discard-", dir=root,
                                     delete=False) as backup:
        backup.write(original)
        if os.geteuid() == 0:
            owner = receipt_path.stat()
            os.chown(backup.name, owner.st_uid, owner.st_gid)
        backup.flush()
        os.fsync(backup.fileno())
    for path in paths:
        # Recheck containment immediately before removal. rmtree never follows interior symlinks.
        if path.resolve() != path or path.is_symlink():
            raise ValueError(f"Partial build changed during cleanup: {path}")
        shutil.rmtree(path)
    receipt.update(state="cancelled", operation="cancelled", services=",".join(services),
                   llama="skip", strata="keep", sdcpp="keep", audiocpp="keep", database="keep",
                   voice=str(int("voice" in services or "voice-gateway" in services)))
    receipt.pop("installer_pid", None)
    receipt.pop("installer_start", None)
    # Preserve the receipt location and permissions, including private legacy receipts.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=receipt_path.parent, delete=False) as output:
            temporary = Path(output.name)
            owner = receipt_path.stat()
            if os.geteuid() == 0:
                os.chown(temporary, owner.st_uid, owner.st_gid)
            os.chmod(temporary, owner.st_mode & 0o777)
            output.write("# Installation plan discarded; retained components remain available.\n")
            output.write("".join(f"{key}={value}\n" for key, value in receipt.items()))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(receipt_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Saved installation choices cancelled. Use owg install to select components again.")


def main():
    """Keep cleanup behind an explicit apply argument for the terminal confirmation flow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        discard(args.root, args.apply)
    except (OSError, ValueError, TypeError) as problem:
        parser.exit(1, f"Could not discard the failed installation: {problem}\n")


if __name__ == "__main__":
    main()
