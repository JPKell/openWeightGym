"""age encryption for recovery archives; only public recovery recipients live on servers."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path


ARCHIVE = "archive.tar.gz.age"
RECIPIENTS = "recipients.txt"


def safe_path(path: Path) -> Path:
    """Reject existing symbolic links in every component of a local maintenance path."""
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (*path.parents, path)):
        raise ValueError("Backup paths must not contain symlinks.")
    return Path(os.path.abspath(path))


def private_directory(path: Path) -> Path:
    """Create an owner-only directory without accepting redirected or foreign-owned roots."""
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError("The backup directory must belong to the service account.")
    path.chmod(0o700)
    return path


def read_regular(path: Path, limit: int, *, private: bool = False) -> bytes:
    """Bound configuration reads and refuse devices, links and writable shared configuration."""
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & (0o077 if private else 0o022)):
            raise ValueError("Backup key/configuration permissions are unsafe; use an owner-only regular file.")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Backup key/configuration exceeds its size limit.")
    return value


def executable(program: str) -> str:
    """Require the maintained age tools; there is no plaintext or custom-crypto fallback."""
    found = shutil.which(program)
    if not found:
        raise ValueError("Install age and age-keygen before creating or recovering encrypted backups.")
    return found


def recipients(root: Path) -> list[str]:
    """Accept native X25519 public recipients only; never identities, SSH keys or plugins."""
    try:
        raw = read_regular(root / RECIPIENTS, 16384).decode("ascii")
    except (OSError, UnicodeError):
        raise ValueError("Backup encryption is not configured: install public age recipients in backups/recipients.txt; keep private keys offline.") from None
    values = list(dict.fromkeys(line.strip() for line in raw.splitlines()
                               if line.strip() and not line.lstrip().startswith("#")))
    if not 1 <= len(values) <= 20 or any(not re.fullmatch(r"age1[0-9a-z]{58}", value) for value in values):
        raise ValueError("Backup recipients.txt must contain 1–20 native age public recipients, one per line; never private keys.")
    # age validates the recipient checksum as well as its shape. Suppress diagnostics
    # because configuration may accidentally contain secret material.
    result = subprocess.run([executable("age"), *(arg for value in values for arg in ("-r", value))],
                            input=b"", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise ValueError("Backup encryption recipients are invalid.")
    executable("age-keygen")
    return values


def encryption_status(root: Path) -> dict:
    """Expose readiness without generating keys or revealing configuration contents."""
    try:
        values = recipients(root)
        return {"ready": True, "recipients": len(values), "reason": "Encrypted backups configured"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ready": False, "recipients": 0,
                "reason": "Encrypted backups need age tools and public recovery recipients. See backup setup in the docs."}


def temporary_identity() -> tuple[bytes, str]:
    """Create a disposable recipient for the automatic ciphertext round trip, never for recovery."""
    result = subprocess.run([executable("age-keygen")], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Could not create the temporary backup verification key.")
    public = subprocess.run([executable("age-keygen"), "-y"], input=result.stdout, capture_output=True, timeout=30)
    if public.returncode:
        raise ValueError("Could not derive the temporary backup verification recipient.")
    return result.stdout, public.stdout.decode("ascii").strip()


def decrypt(archive: Path, identity: Path | bytes, output: Path) -> None:
    """Authenticate the entire ciphertext before a caller extracts or publishes any content."""
    archive = safe_path(archive)
    private = read_regular(identity, 16384, private=True) if isinstance(identity, Path) else identity
    try:
        keys = [line.strip() for line in private.decode('ascii').splitlines()
                if line.strip() and not line.lstrip().startswith('#')]
    except UnicodeError:
        raise ValueError("Use a native age recovery identity file.") from None
    if not 1 <= len(keys) <= 20 or any(not re.fullmatch(r'AGE-SECRET-KEY-1[0-9A-Z]{58}', key) for key in keys):
        raise ValueError("Use a native age recovery identity file; plugins and encrypted identity files are not accepted.")
    if not archive.is_file():
        raise ValueError("The encrypted backup must be a regular file.")
    with output.open("xb") as target:
        os.chmod(output, 0o600)
        result = subprocess.run([executable("age"), "-d", "-i", "-", str(archive)], input=private,
                                stdout=target, stderr=subprocess.DEVNULL, timeout=3600)
    if result.returncode:
        output.unlink(missing_ok=True)
        raise ValueError("Backup decryption/authentication failed; check the recovery key and archive. Nothing was restored.")
