"""The library: every model file in storage, and where else a copy of each one is.

Storage is the complete library. The cache and each compute node hold copies of some of its
files. The Files page's Storage view shows one row per file, with a box for storage, the cache and
each node. A bulk change places a copy where a box was ticked and removes the copy where one was
cleared. A copy is only removed when the file is in storage, and the storage copy itself is never
removed here: that is done one file at a time, on the file's own page.

A file found in the cache or on a node but not in storage is listed too, so it can be kept in
storage. Its copies cannot be removed until it is.

Two copies are the same file when they have the same name, size and description, as the rest of
the Files page counts them. Every copy that is made is checked: a copy made on one drive by its
size, and a copy sent between machines by its SHA-256.

`rows` and `columns` are what the page shows, and `plan` turns the boxes that changed into the
steps that make them so, after checking each against what is in the folders now. They are shared
by weightroom, which draws the boxes, and turnstile, which does the work, so a box the page offers
is a change turnstile accepts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from loadcoach.files import SPARE

STORAGE, CACHE = "storage", "cache"


def identity(file: dict) -> tuple:
    """What makes two copies the same file: name, size and what it says it is."""
    facts = file.get("facts") or {}
    return (Path(file["path"]).name, file.get("size"), facts.get("what", "model"),
            facts.get("name"), facts.get("architecture"), facts.get("size_label"))


def row_id(key: tuple) -> str:
    """A short stable name for one file, used by the page's boxes."""
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


def kind_of(place: str) -> str:
    """The kind of folder a place is (llm, adapters, image…), the same for its cache and its storage."""
    return place.removeprefix("storage_")


def columns(found: dict) -> list[dict]:
    """The places a file can be: storage, the cache when it is a folder of its own, then each compute node."""
    store = found.get("artifact_machine", "front")
    nodes = {node["name"]: node for node in found.get("nodes", [])}
    places = nodes.get(store, {}).get("places", [])
    made = []
    if any(place.get("tier") == STORAGE for place in places):
        made.append({"name": STORAGE, "label": "Storage", "machine": store, "answering": True, "why": ""})
    if any(place.get("tier", CACHE) == CACHE and not place.get("shared") for place in places):
        made.append({"name": CACHE, "label": "Cache", "machine": store, "answering": True, "why": ""})
    for node in found.get("nodes", []):
        if node["name"] in (store, "front"):
            continue
        why = ("" if node.get("answering") and node.get("places") else
               f"{node['name']} is not answering." if not node.get("answering") else
               f"{node['name']} has no model folders. Set them in Settings.")
        made.append({"name": node["name"], "label": node["name"], "machine": node["name"],
                     "answering": bool(node.get("answering")), "why": why})
    return made


def place_for(node: dict, kind: str, tier: str) -> dict | None:
    """The folder on `node` a file of this kind goes into, in the cache or in storage."""
    name = "storage_" + kind if tier == STORAGE else kind
    return next((place for place in node.get("places", []) if place["name"] == name
                 and place.get("tier", CACHE) == tier), None)


def rows(found: dict) -> list[dict]:
    """One row per file: those in storage, by path, then those found only in the cache or on nodes."""
    store = found.get("artifact_machine", "front")
    nodes = {node["name"]: node for node in found.get("nodes", [])}
    made: dict[tuple, dict] = {}

    def add(where: str, machine: str, file: dict) -> None:
        """Count one copy of a file at one of the columns."""
        key = identity(file)
        row = made.setdefault(key, {"id": row_id(key), "name": key[0], "size": file.get("size", 0),
                                    "facts": file.get("facts") or {}, "unreadable": file.get("unreadable", ""), "copies": {}})
        row["copies"].setdefault(where, []).append({**file, "machine": machine})

    for file in nodes.get(store, {}).get("files", []):
        if file.get("tier") == STORAGE or file.get("shared"):
            add(STORAGE, store, file)
        if file.get("tier", CACHE) == CACHE:
            add(CACHE, store, file)
    for name, node in nodes.items():
        if name not in (store, "front"):
            for file in node.get("files", []):
                add(name, name, file)
    for row in made.values():
        row["stored"] = STORAGE in row["copies"]
        primary = (row["copies"].get(STORAGE) or row["copies"].get(CACHE) or next(iter(row["copies"].values())))[0]
        row["path"], row["machine"], row["place"] = primary["path"], primary["machine"], primary.get("place", "")
        row["what"] = row["facts"].get("what", "model")
    return sorted(made.values(), key=lambda row: (not row["stored"], row["path"]))


def cell(row: dict, column: dict, found: dict) -> dict:
    """One box: whether the file is there, and if the box cannot be changed, why not."""
    copies = row["copies"].get(column["name"], [])
    present, why = bool(copies), ""
    used = {model for copy in copies for model in copy.get("used_by") or []}
    if column["machine"] == found.get("artifact_machine"):  # a node on the same machine may load the store's own file
        paths = {copy["path"] for copy in copies}
        used |= {model for node in found.get("nodes", []) if node["name"] != column["machine"]
                 for file in node.get("files", []) if file["path"] in paths for model in file.get("used_by") or []}
    used = sorted(used)
    if column["why"]:
        why = column["why"]
    elif any(copy.get("moving") for copies_at in row["copies"].values() for copy in copies_at):
        why = "It is being copied or moved now."
    elif present and column["name"] == STORAGE:
        why = "In storage. Delete it from storage on the file's own page."
    elif present and not row["stored"]:
        why = "Not in storage, so this may be the only copy. Keep it in storage before removing it here."
    elif present and column["name"] == CACHE and all(copy.get("shared") for copy in copies):
        why = "The cache and storage are one folder here, so this is the storage copy."
    elif present and used:
        why = f"Used by {', '.join(used)}. Change {'that model' if len(used) == 1 else 'those models'} first."
    elif not present:
        why = "" if target(row, column, found) else f"There is no {kind_of(row['place'])} folder for it there."
    return {"present": present, "why": why, "used": used}


def target(row: dict, column: dict, found: dict) -> dict | None:
    """The folder a new copy of the file would go into at this column."""
    nodes = {node["name"]: node for node in found.get("nodes", [])}
    node = nodes.get(column["machine"], {})
    return place_for(node, kind_of(row["place"]), STORAGE if column["name"] == STORAGE else CACHE)


def source(row: dict, column: dict, avoid: set[tuple[str, str]]) -> dict | None:
    """The copy to make a new one from: one on the same machine first, then the cache, storage and other nodes.
    `avoid` holds the (machine, path) of copies about to be removed."""
    order = [CACHE, STORAGE] + [where for where in row["copies"] if where not in (CACHE, STORAGE)]
    copies = [copy for where in order for copy in row["copies"].get(where, [])
              if not copy.get("moving") and (copy["machine"], copy["path"]) not in avoid]
    local = [copy for copy in copies if copy["machine"] == column["machine"]]
    return (local or copies or [None])[0]


def relative(copy: dict, found: dict) -> str:
    """Where a copy is inside its own place's folder, so the new copy goes to the same spot in its own."""
    nodes = {node["name"]: node for node in found.get("nodes", [])}
    folder = next(place["folder"] for place in nodes[copy["machine"]]["places"] if place["name"] == copy["place"])
    return str(Path(copy["path"]).relative_to(folder))


def plan(found: dict, changes: list[dict]) -> list[dict]:
    """The steps for the asked changes, removals first, checked against `found`, a fresh look.

    Each change is {"row": id, "at": column, "keep": bool}. A change to what is already so is left
    out. If any change cannot be made, ValueError says why for every one of them, and nothing is done.
    """
    shown = {column["name"]: column for column in columns(found)}
    listed = {row["id"]: row for row in rows(found)}
    refused, removals, copies = [], [], []
    for change in changes:
        row, column = listed.get(change.get("row")), shown.get(change.get("at"))
        if row is None or column is None:
            refused.append("A file or a place on the page is no longer there. Refresh the page and try again.")
            continue
        box = cell(row, column, found)
        if box["present"] == bool(change.get("keep")):
            continue
        if box["why"]:
            refused.append(f"{row['name']} at {column['label']}: {box['why']}")
            continue
        if box["present"]:
            removals.append({"action": "remove", "row": row["id"], "at": column["name"], "label": column["label"],
                             "file": row["name"], "machine": column["machine"], "size": row["size"],
                             "paths": [copy["path"] for copy in row["copies"][column["name"]]]})
        else:
            copies.append((row, column))
    nodes = {node["name"]: node for node in found.get("nodes", [])}
    removed = {(step["machine"], path) for step in removals for path in step["paths"]}
    room: dict[tuple, dict] = {}
    for step in removals:  # a copy taken off a node leaves room there; one in front's cache may be a second name for its storage copy
        if step["at"] not in (STORAGE, CACHE):
            for copy in listed[step["row"]]["copies"][step["at"]]:
                place = next((one for one in nodes[copy["machine"]]["places"] if one["name"] == copy.get("place")), {})
                drive = room.setdefault((copy["machine"], place.get("filesystem", place.get("folder"))),
                                        {"label": step["label"], "free": place.get("free", 0), "bytes": 0})
                drive["bytes"] -= copy.get("size", 0)
    steps = []
    for row, column in copies:
        origin, place = source(row, column, removed), target(row, column, found)
        if origin is None:
            refused.append(f"{row['name']} at {column['label']}: every copy of it is being removed or moved.")
            continue
        within = relative(origin, found)
        origin_place = next(one for one in nodes[origin["machine"]]["places"] if one["name"] == origin["place"])
        steps.append({"action": "copy", "row": row["id"], "at": column["name"], "label": column["label"],
                      "file": row["name"], "machine": column["machine"], "size": row["size"],
                      "source": origin["machine"], "source_file": {key: origin[key] for key in ("path", "place", "size", "tier") if key in origin},
                      "place": {key: place[key] for key in ("name", "folder", "tier") if key in place},
                      "relative": within, "path": str(Path(place["folder"]) / within)})
        # A copy on one drive of the same machine is a second name for the file and takes no room.
        if origin["machine"] != column["machine"] or origin_place.get("filesystem") != place.get("filesystem"):
            drive = room.setdefault((column["machine"], place.get("filesystem", place["folder"])),
                                    {"label": column["label"], "free": place.get("free", 0), "bytes": 0})
            drive["bytes"] += row["size"]
    for drive in room.values():
        if drive["free"] < drive["bytes"] + SPARE:
            refused.append(f"{drive['label']} has {drive['free'] / 2**30:.1f} GiB free; these copies need "
                           f"{drive['bytes'] / 2**30:.1f} GiB plus 1 GiB to spare.")
    if refused:
        raise ValueError(" ".join(dict.fromkeys(refused)) + " No files were changed.")
    return removals + steps
