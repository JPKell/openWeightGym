"""A kept list of the model files in a machine's places, so the Files page need not read every file each visit.

Reading a file's description (a GGUF header, an image or audio model's facts) is what takes the
time. The index keeps each file's description with its size and modification time, so a new look
only stats the folders and reads the files that are new or have changed.

On a compute node spotter looks again every `files_every` minutes (a per-node setting) and
LoadCoach answers the Files page from the index. LoadCoach puts right the entries for the files it
moves, copies, deletes, downloads or cleans up, and a Refresh on the Files page looks again at
once. Front's own storage, which turnstile keeps, uses the same index under its own name.

The index is a JSON file in the program's data folder. Both programs on a node write it, so every
change happens under a lock on a file beside it and is written whole to a temporary file first.
With no path it is kept in memory only, as tests and settings-less runs do.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from loadcoach.files import model_files_in

VERSION = 1
NODE_INDEX = "files-index.json"  # LoadCoach's data folder, on each node
FRONT_INDEX = "front-files-index.json"  # turnstile's data folder: on one machine it may be LoadCoach's as well


def describe(path: Path) -> dict:
    """What a model, image or audio file says about itself, read without loading it."""
    from loadcoach.gguf import facts, read_metadata
    from mirrorwall.assets import facts as image_facts, image_asset
    from soundroom.model import audio_asset, facts as audio_facts
    if 'classifier' in path.parts and path.suffix.lower() != '.gguf':
        from loadcoach.classifier_sources import source_facts
        return source_facts(path)
    if audio_asset(path):
        return audio_facts(path)
    if image_asset(path):
        return image_facts(path)
    return facts(read_metadata(str(path)))


def listed_in(folder: Path, path: Path) -> bool:
    """Whether a look through `folder` would list `path`, by the same rule as model_files_in, without listing the folder."""
    from mirrorwall.assets import supported
    try:
        depth = len(path.resolve().relative_to(folder.resolve()).parts)
    except ValueError:
        return False
    return supported(path) and depth <= (6 if folder.name in ("image", "audio", "encoders", "classifier") else 2)


def tier_of(place: str) -> str:
    """A place named storage_NAME is the library; every other place is the cache."""
    return "storage" if place.startswith("storage_") else "cache"


class FileIndex:
    """The files of a set of places, with their descriptions, kept between looks."""

    def __init__(self, path: str | Path | None, describe: Callable[[Path], dict] = describe,
                 accept: Callable[[str], object] | None = None):
        """`accept` may refuse a file found in a place by raising ValueError, as front's path check does."""
        self.path = Path(path) if path else None
        self.describe, self.accept = describe, accept
        self.kept: dict | None = None  # the index itself when there is no file
        self.local = threading.Lock()  # one thread of this program at a time; the file lock covers the other program

    @contextmanager
    def locked(self):
        """Hold the index for one change, against this program's other threads and the other program."""
        with self.local:
            if self.path is None:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> dict | None:
        """The index as last written, or None if there is none or it cannot be used."""
        if self.path is None:
            return self.kept
        try:
            index = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(index, dict) or index.get("version") != VERSION or not isinstance(index.get("files"), dict):
            return None
        return index

    def write(self, index: dict) -> None:
        """Replace the index whole, so a reader never sees half of one."""
        if self.path is None:
            self.kept = index
            return
        part = self.path.with_name(f"{self.path.name}.{os.getpid()}.part")
        part.write_text(json.dumps(index))
        os.replace(part, self.path)

    def entry(self, file: Path, place: str, seen: os.stat_result, before: dict | None) -> dict:
        """One file's line: the one kept before if the file has not changed since, otherwise read again."""
        facts = (before or {}).get('facts', {})
        from mirrorwall.components import IDENTITY_VERSION
        needs_image_identity = (facts.get('what') == 'image' or facts.get('image_component')) and facts.get('image_identity', {}).get('version') != IDENTITY_VERSION
        if (before and before.get("place") == place and before.get("size") == seen.st_size
                and before.get("mtime") == seen.st_mtime and ("facts" in before or "unreadable" in before)
                and not needs_image_identity):
            return before
        entry = {"path": str(file), "place": place, "tier": tier_of(place), "size": seen.st_size, "mtime": seen.st_mtime,
                 "modified": datetime.fromtimestamp(seen.st_mtime).isoformat(timespec="seconds")}
        try:
            entry["facts"] = self.describe(file)
        except (OSError, ValueError) as problem:
            entry["unreadable"] = str(problem)
        return entry

    def scan(self, spots: dict[str, str]) -> dict:
        """Look through every place again. Only new and changed files are read."""
        with self.locked():
            old = (self.read() or {}).get("files", {})
            files, problems = {}, []
            for place, folder in spots.items():
                if not Path(folder).is_dir():
                    continue  # said by whoever lists the places, which looks at them each time
                try:
                    for file in model_files_in(folder):
                        if str(file) in files:
                            continue  # cache and storage set to one folder: listed once, under the cache place, which comes first
                        if self.accept is not None:
                            self.accept(str(file))
                        try:
                            seen = file.stat()
                        except OSError:
                            continue  # it went between being listed and being looked at, as a file just moved does
                        files[str(file)] = self.entry(file, place, seen, old.get(str(file)))
                except (OSError, ValueError) as problem:
                    problems.append(f"{folder}: {problem}")
            index = {"version": VERSION, "checked": time.time(), "places": dict(spots), "files": files, "problems": problems}
            self.write(index)
            return index

    def current(self, spots: dict[str, str], *, refresh: bool = False, every: float = 0) -> dict:
        """The index to answer from: looked through again if asked, if there is none yet, if the places
        changed since, if something said it is out of date, or if it is older than `every` seconds (0: never)."""
        index = None if refresh else self.read()
        if (index is None or index.get("places") != dict(spots) or index.get("stale")
                or (every and time.time() - index.get("checked", 0) > every)):
            index = self.scan(spots)
        return index

    def update(self, paths: list[str | Path], spots: dict[str, str]) -> None:
        """Put right the lines for files just made, changed or removed. Without an index, or with other
        places than these, nothing is changed: the next look reads everything anyway."""
        with self.locked():
            index = self.read()
            if index is None or index.get("places") != dict(spots):
                return
            # The deepest folder holding the file names its place; between two places on one folder, the first, as a look does.
            roots = sorted(((name, Path(folder).resolve()) for name, folder in spots.items()), key=lambda one: -len(one[1].parts))
            for path in map(Path, paths):
                old = index["files"].pop(str(path), None)
                try:
                    seen = path.stat()
                    if path.is_symlink() or not path.is_file():
                        continue
                    if self.accept is not None:
                        self.accept(str(path))
                except (OSError, ValueError):
                    continue  # gone, or not a file the places may hold: its line stays out
                place = next((name for name, root in roots if root in path.resolve().parents), None)
                if place is not None and listed_in(Path(spots[place]), path):
                    index["files"][str(path)] = self.entry(path, place, seen, old)
            self.write(index)

    def stale(self) -> None:
        """Say the index is out of date, after a change too wide to put right line by line."""
        with self.locked():
            index = self.read()
            if index is not None:
                self.write({**index, "stale": True})

    @staticmethod
    def checked(index: dict) -> str:
        """When the index was last looked through, as an ISO time in UTC."""
        return datetime.fromtimestamp(index.get("checked", 0), tz=timezone.utc).isoformat(timespec="seconds")
