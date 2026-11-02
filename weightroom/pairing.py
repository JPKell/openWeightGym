"""Pairing: a GPU node joins this console with a one-time code instead of a copied key.

An administrator makes a code, under Settings → Compute nodes or with `weightroom pair`. Within
ten minutes the node's installer sends it to POST /pair. The console answers with the machine key
the two will sign with, the name the node goes by and the address of turnstile, and records the
node in settings/weightroom.toml: as the main compute node when there is none yet, otherwise as a
further [machines.NAME] with a key of its own. Turnstile and weightroom then restart to use it.

A code works once. Codes are kept only as hashes, in the data folder, beside a lock that keeps the
console and a `weightroom pair` from writing at the same moment.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import subprocess
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from weightroom.settings_file import save_settings, set_toml_value

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0 and O, no 1, I and L
MINUTES = 10
INSTALLER = "https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def normal(code: str) -> str:
    """A code as typed, with dashes, spaces and lower case taken out."""
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def digest(code: str) -> str:
    """What is kept of a code: its hash."""
    return hashlib.sha256(normal(code).encode()).hexdigest()


@contextmanager
def held(folder: Path):
    """The waiting codes, locked while in hand; expired ones are dropped on the way."""
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "pairing.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / "pairing.json"
        try:
            codes = json.loads(path.read_text())
        except (OSError, ValueError):
            codes = {}
        now = time.time()
        codes = {key: entry for key, entry in codes.items() if entry.get("expires", 0) > now}
        yield codes
        path.write_text(json.dumps(codes))
        path.chmod(0o600)


def make_code(folder: Path, made_by: str, minutes: int = MINUTES) -> tuple[str, float]:
    """A new code, shown once as XXXX-XXXX, and when it stops working."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    expires = time.time() + minutes * 60
    with held(Path(folder)) as codes:
        codes[digest(code)] = {"expires": expires, "made_by": made_by}
    return f"{code[:4]}-{code[4:]}", expires


def take_code(folder: Path, code: str) -> bool:
    """Use a code up. True only for one that was waiting and is still in date."""
    if len(normal(code)) != 8:
        return False
    with held(Path(folder)) as codes:
        return codes.pop(digest(code), None) is not None


def valid_address(address: str) -> bool:
    """An IP address or a host name: what goes into a URL as the node's host."""
    try:
        ipaddress.ip_address(address)
        return True
    except ValueError:
        return bool(HOST.fullmatch(address))


def url_host(address: str) -> str:
    """The address as a URL writes it: an IPv6 one in brackets."""
    return f"[{address}]" if ":" in address else address


def node_name(wanted: str, taken: set[str]) -> str:
    """A name for a further node: the one it asked for, made safe and unique, or node-2, node-3..."""
    base = re.sub(r"[^a-z0-9-]+", "-", wanted.lower()).strip("-")[:28] or "node"
    if not NAME.fullmatch(base) or base == "compute":
        base = "node"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def join(settings_path: Path, address: str, wanted: str = "", voice: bool = False) -> dict:
    """Record the node at `address` and say which name and machine key it signs with.

    With no compute node yet (compute names this machine and no LoadCoach runs here) it becomes
    the main one, signing with the machine key turnstile already holds. Otherwise it becomes a
    further node with a new key of its own. A node that pairs again from the same address keeps
    its name and key.
    """
    folder = settings_path.parent
    original = settings_path.read_text()
    told = tomllib.loads(original)
    text = original
    host = url_host(address)
    compute = urlsplit(told.get("compute", "")).hostname or ""
    machines = told.get("machines", {})
    turnstile = told.get("turnstile", {})
    if voice:
        ports = list(turnstile.get("ports", [8442, 8443, 8444]))
        voice_port = turnstile.get("voice_port", 8445)
        if voice_port not in ports:
            text = set_toml_value(text, "turnstile.ports", [*ports, voice_port])
    none_yet = compute in LOOPBACK and not (folder / "loadcoach.toml").exists()
    primary = none_yet or compute == address
    if primary:
        name = "compute"
        key_file = folder / turnstile.get("machine_key_file", "../keys/machine.key")
        text = set_toml_value(text, "compute", f"http://{host}:8442")
        text = set_toml_value(text, "spotter", f"http://{host}:8444")
    else:
        name = next((known for known, machine in machines.items()
                     if urlsplit(machine.get("compute", "")).hostname == address), "")
        if name:
            key_file = folder / machines[name].get("machine_key_file", "../keys/machine.key")
            # Repair a legacy collision without copying an API key into a signing-key file.
            reserved = {folder / turnstile.get("machine_key_file", "../keys/machine.key"),
                        folder / told.get("key_file", "../keys/weightroom.key"),
                        *(folder / f"../keys/{service}.key" for service in ("voice-service", "voice-gateway", "voice-browser"))}
            if key_file.resolve() in {path.resolve() for path in reserved}:
                key_file = folder / f"../keys/machines/{name}.key"
                text = set_toml_value(text, f"machines.{name}.machine_key_file", f"../keys/machines/{name}.key")
        else:
            name = node_name(wanted, set(machines))
            key_file = folder / f"../keys/machines/{name}.key"
            for key, value in (("compute", f"http://{host}:8442"), ("spotter", f"http://{host}:8444"),
                               ("machine_key_file", f"../keys/machines/{name}.key")):
                text = set_toml_value(text, f"machines.{name}.{key}", value)
    key_file = key_file.resolve()
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_file.touch(mode=0o600)
        key_file.write_text(secrets.token_urlsafe(32) + "\n")
    changed = save_settings(settings_path, text, "pairing")
    gateway = folder / "voice-gateway.toml"
    if primary and gateway.is_file():
        voice_text = set_toml_value(gateway.read_text(), "voice", f"http://{host}:8445")
        changed = save_settings(gateway, voice_text, "pairing") or changed
    return {"name": name, "machine_key": key_file.read_text().strip(), "changed": changed}


def restart_later(units: tuple[str, ...] = ("turnstile.service", "weightroom.service"), seconds: int = 3) -> bool:
    """Restart turnstile and weightroom a few seconds from now, after this answer has gone."""
    try:
        subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--on-active={seconds}",
                        "systemctl", "--user", "restart", *units], check=True, capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def lan_address() -> str:
    """This machine's address on its network: the one a route out would leave from."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def console_url(host: str, port: int) -> str:
    """The console as another machine reaches it: never as localhost."""
    if not host or host in LOOPBACK:
        host = lan_address()
    return f"http://{url_host(host)}:{port}"


def command(console: str, code: str) -> str:
    """What to run on the GPU machine."""
    return f"curl -fsSL {INSTALLER} | bash -s -- --join {console} --code {code}"
