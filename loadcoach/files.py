"""Model files on the drives: finding them, and carrying one from one place to another.

A place is a folder that holds model files, with a name, such as `fast` for the folder on the
quick drive and `slow` for the one on the roomy drive. Nothing here knows about models, users or
the web. It only looks at folders and copies bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

SPARE = 2**30  # a file is only carried to a drive that will still have this much free afterwards
PIECE = 16 * 2**20  # how much is copied at a time, and so how often the progress moves on


def model_files_in(folder: str) -> list[Path]:
    """Find model data; image bundles may have nested component directories."""
    from mirrorwall.assets import supported
    root=Path(folder)
    if root.name in ('image', 'audio', 'encoders'):
        return sorted(file for file in root.rglob('*') if file.is_file() and not file.is_symlink() and supported(file) and len(file.relative_to(root).parts)<=6)
    return sorted(file for pattern in ('*','*/*') for file in root.glob(pattern) if file.is_file() and not file.is_symlink() and supported(file))


def sha256_of(path: str, stop=None) -> str:
    """The SHA-256 of a whole file. Raises InterruptedError if `stop`, a threading.Event, is set meanwhile.

    A model file takes seconds to read through, and this runs on a thread of its own. A thread
    cannot be ended from outside, so it looks at `stop` between pieces: LoadCoach sets it when it
    shuts down, and does not have to wait for a file to be read to its end.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while piece := file.read(PIECE):
            if stop is not None and stop.is_set():
                raise InterruptedError
            digest.update(piece)
    return digest.hexdigest()


def room(folder: str) -> dict:
    """How big the drive under a folder is, and how much of it is free, in bytes."""
    drive = shutil.disk_usage(folder)
    return {"total": drive.total, "free": drive.free, "filesystem": str(os.stat(folder).st_dev)}


def same_drive(file: Path, folder: Path) -> bool:
    """True if a file and a folder are on one drive, where a file can be given a second name without copying it."""
    return os.stat(file).st_dev == os.stat(folder).st_dev


def carry(source: Path, target: Path, progress: dict) -> None:
    """Put a copy of `source` at `target`, leaving `source` where it is. `progress["done"]` counts the bytes.

    Afterwards the file is in both places, so whoever uses it can be pointed at the new one
    before the old one is taken away. Raises OSError if it cannot be done, leaving nothing behind.
    """
    if same_drive(source, target.parent):
        os.link(source, target)  # a second name for the same file, made in an instant
        progress["done"] = progress["of"]
        return
    descriptor, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=target.parent)
    part = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as new, open(source, "rb") as old:
            while piece := old.read(PIECE):
                new.write(piece)
                progress["done"] += len(piece)
            new.flush()
            os.fsync(new.fileno())  # truly on the drive before the original may go
        if part.stat().st_size != source.stat().st_size:
            raise OSError("the copy is not the size of the original")
        shutil.copystat(source, part)  # keeps the file's date, which is how it is known to be the same file
        # Publish without overwriting a target created after the caller's preflight.
        # The private temporary name also prevents concurrent copies sharing a writer.
        os.link(part, target)
        part.unlink()
    except BaseException:
        part.unlink(missing_ok=True)
        raise
