"""Node-local installed versions, shared by Components, Spotter and the installer."""

import asyncio
import math
import re
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

from db import database, migrate
from db.schemas import COMPONENT_VERSIONS

ROOT = Path(__file__).resolve().parents[1]
EVERY = 60 * 60


def interval_seconds(minutes) -> float:
    """Validate the live version-check interval before using or saving it."""
    if type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("component_versions_every must be a finite number of minutes above 0")
    return minutes * 60


def git_version(root: Path) -> str:
    """Nearest release tag and the actual checkout commit, including untagged checkouts."""
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = git("rev-parse", "--short=12", "HEAD")
    if not commit:
        return ""
    tag = git("describe", "--tags", "--abbrev=0")
    return f"{tag} ({commit})" if tag else commit


def engine_config(path: Path, source=None):
    """Read only engine settings, including database overrides, without starting LoadCoach."""
    told = tomllib.loads(path.read_text())
    if source is not None:
        from loadcoach.settings import changed_for, laid_over
        told = laid_over(told, changed_for("loadcoach", source), "loadcoach")
    return SimpleNamespace(engines=told.get("engines", {}), llama_server=told.get("llama_server", "llama-server"))


async def reported_version(executable: str) -> str:
    """Ask a native engine for its version, with bounded output and process cleanup."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        async with asyncio.timeout(15):
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > 16384:
                    raise ValueError("Engine version output was too long.")
            await process.wait()
        if process.returncode:
            raise ValueError(f"Engine version command exited with status {process.returncode}.")
        lines = [line.strip() for line in output.decode(errors="replace").splitlines() if line.strip()]
        # llama.cpp can print backend discovery before its actual version line.
        preferred = ([line for line in lines if re.match(r"version\s*:", line, re.I)]
                     or [line for line in lines if re.search(r"audio\.cpp|stable-diffusion\.cpp|version", line, re.I)])
        version = re.sub(r"^version\s*:\s*", "", next(iter(preferred or lines), ""), flags=re.I)
        if not version:
            raise ValueError("Engine did not report a version.")
        return version[:512]
    finally:
        if process is not None and (process.returncode is None or not process.stdout.at_eof()):
            import os
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


class Versions:
    """One persisted suite identity per host, and one reported identity per installed engine."""

    def __init__(self, source, root: Path = ROOT, *, minutes=EVERY / 60):
        self.every = interval_seconds(minutes)
        self.source, self.root = database(source), root
        self.db = self.source.connect(check_same_thread=False)
        migrate(self.source, self.db, "components.versions", COMPONENT_VERSIONS)
        self.hostname = socket.gethostname()
        # The database cache survives restarts; each new process must verify its checkout.
        self.suite_checked = False

    def read(self) -> dict:
        return {name: {"version": version, "version_checked": checked, "version_error": error}
                for name, version, checked, error in self.db.execute(
                    "SELECT component,version,checked,error FROM component_versions WHERE hostname=?", (self.hostname,))}

    def save(self, name: str, version: str, error: str = "") -> None:
        with self.db:
            self.db.execute("INSERT INTO component_versions(hostname,component,version,checked,error) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(hostname,component) DO UPDATE SET version=excluded.version,checked=excluded.checked,error=excluded.error",
                            (self.hostname, name, version, time.time(), error))

    async def suite(self) -> None:
        try:
            version = await asyncio.to_thread(git_version, self.root)
            self.save("suite", version, "Git tag/commit unavailable." if not version else "")
        except (OSError, subprocess.SubprocessError) as problem:
            self.save("suite", "", str(problem))
        self.suite_checked = True

    async def refresh(self, config=None, settings: Path | None = None, force=False) -> None:
        """Refresh due versions; each failure is isolated so every engine gets checked."""
        before = self.read()
        due = lambda name: force or time.time() - before.get(name, {}).get("version_checked", 0) >= self.every
        if not self.suite_checked or due("suite"):
            await self.suite()
        if config is None:
            if settings is None or not settings.is_file():
                return
            config = engine_config(settings, self.source)
        from engines import _ENGINES
        import shutil
        for name, engine in _ENGINES.items():
            if not due(name):
                continue
            try:
                installation = engine.installation(config)
                if not installation.executable or not shutil.which(installation.executable):
                    self.save(name, "")
                    continue
                # Strata's native worker has no version option; use its own source checkout.
                if name == "strata":
                    source = engine.node(config).source
                    version = await asyncio.to_thread(git_version, Path(source)) if source else ""
                    if not version:
                        raise ValueError("Strata source revision unavailable.")
                else:
                    version = await reported_version(installation.executable)
                self.save(name, version)
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as problem:
                self.save(name, "", str(problem)[:512])

    async def watch(self, *, settings=None, minutes, gate) -> None:
        """Check independently of page visits, respecting maintenance and live settings."""
        while True:
            await gate.wait()
            try:
                self.every = interval_seconds(minutes())
                await self.refresh(settings=settings)
            except (OSError, ValueError) as problem:
                print(f"Component versions could not be checked: {problem}", file=sys.stderr, flush=True)
            # Re-read settings within a minute, even when the configured interval is longer.
            await asyncio.sleep(min(self.every, 60))

    async def attach(self, report: dict) -> dict:
        """Add saved versions to a live inventory; page requests never invoke an engine."""
        saved = self.read()
        if not self.suite_checked or time.time() - saved.get("suite", {}).get("version_checked", 0) >= self.every:
            await self.suite()
            saved = self.read()
        for kind in ("services", "engines"):
            for row in report[kind]:
                row.update(saved.get("suite" if kind == "services" else row["id"],
                                     {"version": "", "version_checked": None, "version_error": "Not checked yet."})
                             if row.get("installed") else {"version": "", "version_checked": None, "version_error": ""})
        return report


async def update_installation(root: Path) -> None:
    """Record versions after installation, using the same store as the Components API."""
    from db import DatabaseRegistry
    from loadcoach.config import data_folder, kept_by_loadcoach, read_settings
    compute = root / "settings/loadcoach.toml"
    front = root / "settings/weightroom.toml"
    if compute.is_file():
        source = kept_by_loadcoach(compute)[1]
    elif front.is_file():
        told = read_settings(front)
        source = DatabaseRegistry.from_config(told.get("databases"), front.parent,
                    {"weightroom.accounts": data_folder(front, told) / "accounts.db"}).database("weightroom.accounts")
    else:
        return
    versions = Versions(source, root)
    try:
        await versions.refresh(settings=compute, force=True)
    finally:
        versions.db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    asyncio.run(update_installation(parser.parse_args().root))
