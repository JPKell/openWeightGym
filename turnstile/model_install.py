"""Verified model-bundle installation using Files inventories and existing transfers."""
import contextlib
from pathlib import Path

from engines import engine_for
from loadcoach.config import kind_of
from loadcoach.files import SPARE
from turnstile.library import place_for
from turnstile.model_configuration import relocate


def storage_place(node, file):
    """The destination node's model storage folder for this kind of file."""
    place = place_for(node, kind_of(file["place"]))
    if place is None:
        raise ValueError(f"{node['name']} has no {kind_of(file['place'])} model storage folder for {Path(file['path']).name}.")
    return place


def relative_path(node, file):
    """Keep nested image components and audio packages together in their folder."""
    root = next(place["folder"] for place in node["places"] if place["name"] == file["place"])
    return str(Path(file["path"]).relative_to(root))


def plan(settings, nodes, destination, source_machine=""):
    """Resolve every file and check aggregate free space before any copying or model creation.

    A file already on the destination node is used where it is. Any other is copied once, into
    the destination's model storage."""
    target = next((node for node in nodes if node["name"] == destination and node.get("answering")), None)
    if target is None:
        raise ValueError(f"Compute node {destination} is not answering; no files or models were changed.")
    steps, paths, allocations = [], {}, {}
    for path in dict.fromkeys(engine_for(settings).model.files_of(settings).values()):
        sources = [(node, file) for node in nodes for file in node.get("files", []) if file["path"] == path and not file.get("moving")]
        if not sources:
            raise ValueError(f"{path} is not in model storage on any answering node. No files or models were changed.")
        here = next(((node, file) for node, file in sources if node["name"] == destination), None)
        if here:
            paths[path] = path  # already in the destination's own storage
            continue
        source, file = next(((node, file) for node, file in sources if node["name"] == source_machine), sources[0])
        relative = relative_path(source, file)
        destination_place = storage_place(target, file)
        target_path = str(Path(destination_place["folder"]) / relative)
        steps.append({"source": source["name"], "file": file, "node": destination, "place": destination_place,
                      "relative": relative, "path": target_path})
        paths[path] = target_path
    unique = {}
    for step in steps:
        key = (step['node'], step['path'])
        prior = unique.get(key)
        if prior and (prior['source'], prior['file']['path']) != (step['source'], step['file']['path']):
            raise ValueError(f"Two selected model files would collide at {step['node']}: {step['path']}.")
        unique[key] = step
    for step in unique.values():
        node = next(one for one in nodes if one["name"] == step["node"])
        existing = next((file for file in node["files"] if file["path"] == step["path"]), None)
        step["existing"] = existing
        if existing and existing["size"] != step["file"]["size"]:
            raise ValueError(f"{step['node']} already has a different file at {step['path']}.")
        if not existing:
            place = step["place"]
            key = (step["node"], place.get("filesystem", place["folder"]))
            allocation = allocations.setdefault(key, {"node": step["node"], "place": place["name"], "free": place["free"], "bytes": 0})
            allocation["bytes"] += step["file"]["size"]
    for allocation in allocations.values():
        if allocation["free"] < allocation["bytes"] + SPARE:
            raise ValueError(f"Not enough model storage space on {allocation['node']} ({allocation['place']}): "
                             f"{allocation['bytes'] / 2**30:.2f} GiB needed, {allocation['free'] / 2**30:.2f} GiB free, "
                             "plus 1 GiB spare. No files or models were changed.")
    return {"steps": list(unique.values()), "settings": relocate(settings, paths), "allocations": allocations}


async def execute(prepared, digest, transfer, job, claim=None):
    """Verify collisions before mutation, then transfer complete files in order.

    `claim(node, path)`, if given, is held while a file is placed. Installations to several models
    can share a step, such as one file copied to a node; the one that waited finds the file there
    already, with the same SHA-256, and goes on without placing it again.
    """
    hashes = {}
    async def verify(machine, path, size):
        """Publish hashing before its first await; large bundles must not look queued."""
        job.update(state='verifying', working_machine=machine, file=Path(path).name,
                   done=0, of=size, progress_available=False)
        return await digest(machine, path)
    # Resolve original hashes first.
    for step in prepared["steps"]:
        key = (step["source"], step["file"]["path"])
        if key not in hashes:
            prior = next((one for one in prepared["steps"] if (one["node"], one["path"]) == key), None)
            hashes[key] = hashes[(prior["source"], prior["file"]["path"])] if prior else await verify(*key, step['file']['size'])
        if step["existing"] and await verify(step["node"], step["path"], step['existing']['size']) != hashes[key]:
            raise ValueError(f"The file already on {step['node']} does not match the selected file's SHA-256: {step['path']}.")
    for step in prepared["steps"]:
        if step["existing"]:
            continue
        wanted = hashes[(step["source"], step["file"]["path"])]
        job.update(state='waiting', working_machine=step['node'], file=Path(step['path']).name,
                   done=0, of=0, progress_available=False)
        async with claim(step["node"], step["path"]) if claim else contextlib.nullcontext():
            if claim:
                try:
                    if await verify(step["node"], step["path"], step['file']['size']) == wanted:
                        continue  # another installation placed this file while this one waited
                except ValueError:
                    pass  # not there yet
            job.update(state="copying", file=Path(step["path"]).name,
                       working_machine=step["node"], done=0, of=step["file"]["size"], progress_available=True)
            await transfer(step, wanted, job)
            if await verify(step["node"], step["path"], step['file']['size']) != wanted:
                raise ValueError(f"The installed file failed SHA-256 verification: {step['path']}.")
    return prepared["settings"]
