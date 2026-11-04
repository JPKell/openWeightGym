"""Which openWeightGym services and engines a machine has, for the console's System → Components page.

A service counts as installed when systemd knows its unit, in this account's own manager or the
system one (where system-mode installations put theirs). An engine counts as installed when its
configured program is found. Nothing here starts, stops or changes anything, and no engine is run.
"""

import asyncio
import shutil
import socket
import tomllib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

# Every service the installer can set up: its name, what it is, and where it usually runs.
SERVICES = (
    ("weightroom", "Web console", "front"),
    ("turnstile", "API gateway", "front"),
    ("voice-gateway", "Voice gateway", "front"),
    ("loadcoach", "Model runner", "compute"),
    ("toollocker", "Tools", "compute"),
    ("spotter", "Monitor", "compute"),
    ("voice", "Voice", "compute"),
)
FORMER_UNITS = {"weightroom": "frontdesk.service", "toollocker": "tooldoor.service"}  # names from before the rename
RECEIPT_FIELDS = ("topology", "services", "mode", "installed", "state")  # never its URLs, accounts or paths
STATES = {"active": "running", "inactive": "stopped", "failed": "failed", "activating": "starting",
          "deactivating": "stopping", "reloading": "reloading", "masked": "masked"}


async def units(names: list[str], own: bool) -> dict[str, dict] | None:
    """What systemd says of each unit, by unit name, from this account's manager or the system's. None if it cannot be asked."""
    try:
        asked = await asyncio.create_subprocess_exec(
            "systemctl", "--user" if own else "--no-ask-password", "show", *names, "-p", "Id,LoadState,ActiveState,SubState",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        printed, _ = await asyncio.wait_for(asked.communicate(), 10)
    except (OSError, TimeoutError):
        return None
    if asked.returncode:
        return None
    found = {}
    for block in printed.decode(errors="replace").split("\n\n"):
        told = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if told.get("Id"):
            found[told["Id"]] = told
    return found


async def services(running_here: tuple[str, ...] = ()) -> list[dict]:
    """Every service, installed or not. `running_here` are the ones answering this question, which run even without a unit."""
    names = [name + ".service" for name, _, _ in SERVICES] + list(FORMER_UNITS.values())
    own, system = await asyncio.gather(units(names, True), units(names, False))
    rows = []
    for name, label, usual in SERVICES:
        row = {"id": name, "label": label, "usual": usual, "installed": False, "unit": "", "scope": "", "state": "", "detail": ""}
        for scope, found in (("user", own), ("system", system)):
            for unit in (name + ".service", FORMER_UNITS.get(name)):
                told = (found or {}).get(unit or "", {})
                if unit and told.get("LoadState", "not-found") != "not-found":
                    active = told.get("ActiveState", "") if told.get("LoadState") == "loaded" else told.get("LoadState", "")
                    row.update(installed=True, unit=unit, scope=scope, state=STATES.get(active, active or "unknown"),
                               detail=told.get("SubState", ""))
                    break
            if row["installed"]:
                break
        if not row["installed"] and name in running_here:
            row.update(installed=True, state="running", detail="running without a systemd unit")
        elif not row["installed"] and own is None and system is None:
            row["installed"] = None  # systemd could not be asked: unknown, not absent
        rows.append(row)
    return rows


def engines(config) -> list[dict]:
    """Every engine, with whether its program is here, whether it is turned on, and why it cannot be used if it cannot."""
    from engines import catalogue
    rows = []
    for engine in catalogue(config):
        executable = engine.get("executable") or (engine.get("settings") or {}).get("executable") or ""
        present = bool(executable and shutil.which(executable))
        rows.append({"id": engine["id"], "label": engine["label"], "installed": present,
                     "enabled": bool(engine.get("enabled", True)), "available": bool(engine.get("available")),
                     "why": engine.get("why", "") if present else ""})
    return rows


def engines_from_settings(path: Path) -> list[dict]:
    """Engines as LoadCoach's settings file on this machine describes them; none installed when there is no such file.

    For front, which reads the file without starting LoadCoach or opening its database.
    """
    try:
        told = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        told = None
    if told is None:
        return [{**engine, "installed": False, "enabled": False, "available": False, "why": ""} for engine in everything()["engines"]]
    config = SimpleNamespace(engines=told.get("engines", {}) if isinstance(told.get("engines"), dict) else {},
                             llama_server=str(told.get("llama_server", "llama-server")))
    try:
        return engines(config)
    except (ValueError, TypeError, OSError) as problem:
        return [{**engine, "installed": None, "enabled": False, "available": False,
                 "why": f"LoadCoach's settings could not be read: {problem}"} for engine in everything()["engines"]]


def everything() -> dict:
    """Every service and engine there is, by id and name: what a machine that does not answer is listed with."""
    from engines import _ENGINES
    return {"services": [{"id": name, "label": label, "usual": usual} for name, label, usual in SERVICES],
            "engines": [{"id": engine.id, "label": engine.label} for engine in _ENGINES.values()]}


def receipt(root: Path = ROOT) -> dict | None:
    """What install.sh recorded when it made this installation, if it did: only the fields worth showing."""
    try:
        lines = (root / ".install-receipt").read_text().splitlines()
    except OSError:
        return None
    told = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    return {field: told.get(field, "") for field in RECEIPT_FIELDS}


async def here(config=None, running_here: tuple[str, ...] = (), settings: Path | None = None) -> dict:
    """This machine's services, engines and install receipt. `config` is LoadCoach's own; without it, `settings` is read."""
    return {"hostname": socket.gethostname(), "services": await services(running_here),
            "engines": engines(config) if config is not None else engines_from_settings(settings or ROOT / "settings/loadcoach.toml"),
            "receipt": receipt()}
