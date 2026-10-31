"""Verified model-bundle installation using Files inventories and existing transfers."""
from pathlib import Path

from engines import engine_for
from loadcoach.files import SPARE
from turnstile.model_configuration import relocate


def cache_place(node, file):
    """Use the matching cache, never a storage tier or an arbitrary storage folder."""
    name = file["place"].removeprefix("storage_")
    caches = [place for place in node["places"] if place.get("tier", "cache") == "cache"]
    place = next((one for one in caches if one["name"] == name), None)
    if place is None:
        raise ValueError(f"{node['name']} has no active cache named {name} for {file['place']}.")
    return place


def relative_path(node, file):
    """Keep nested image components and audio packages together in their cache."""
    root = next(place["folder"] for place in node["places"] if place["name"] == file["place"])
    return str(Path(file["path"]).relative_to(root))


def plan(settings, nodes, destination, source_machine=""):
    """Resolve every file and check aggregate free space before any copying or model creation."""
    target = next((node for node in nodes if node["name"] == destination and node.get("answering")), None)
    if target is None:
        raise ValueError(f"Compute node {destination} is not answering; no files or models were changed.")
    steps, paths, allocations = [], {}, {}
    for path in dict.fromkeys(engine_for(settings).model.files_of(settings).values()):
        sources = [(node, file) for node in nodes for file in node.get("files", []) if file["path"] == path and not file.get("moving")]
        sources.sort(key=lambda pair: (pair[0]["name"] != (source_machine or destination), pair[1].get("tier") == "storage"))
        if not sources:
            raise ValueError(f"{path} is not available in cache/storage. No files or models were changed.")
        source, file = sources[0]
        relative = relative_path(source, file)
        cache = cache_place(source, file)
        cached_path = str(Path(cache["folder"]) / relative)
        if file.get("tier") == "storage":
            steps.append({"source": source["name"], "file": file, "node": source["name"], "place": cache,
                          "relative": relative, "path": cached_path, "promotion": True})
            file = {**file, "path": cached_path, "place": cache["name"], "tier": "cache"}
        destination_place = cache_place(target, file)
        target_path = str(Path(destination_place["folder"]) / relative)
        if source["name"] != destination or file["path"] != target_path:
            steps.append({"source": source["name"], "file": file, "node": destination, "place": destination_place,
                          "relative": relative, "path": target_path, "promotion": False})
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
            raise ValueError(f"Not enough cache space on {allocation['node']} ({allocation['place']}): "
                             f"{allocation['bytes'] / 2**30:.2f} GiB needed, {allocation['free'] / 2**30:.2f} GiB free, "
                             "plus 1 GiB spare. No files or models were changed.")
    return {"steps": list(unique.values()), "settings": relocate(settings, paths), "allocations": allocations}


async def execute(prepared, digest, promote, transfer, job):
    """Verify collisions before mutation, then promote and transfer complete files in order."""
    hashes = {}
    # Resolve original hashes first; a promoted source may not exist yet.
    for step in prepared["steps"]:
        key = (step["source"], step["file"]["path"])
        if key not in hashes:
            prior = next((one for one in prepared["steps"] if (one["node"], one["path"]) == key), None)
            hashes[key] = hashes[(prior["source"], prior["file"]["path"])] if prior else await digest(*key)
        if step["existing"] and await digest(step["node"], step["path"]) != hashes[key]:
            raise ValueError(f"The cache file on {step['node']} does not match the selected file's SHA-256: {step['path']}.")
    for step in prepared["steps"]:
        if step["existing"]:
            continue
        job.update(state="promoting" if step["promotion"] else "copying", file=Path(step["path"]).name,
                   machine=step["node"], done=0, of=step["file"]["size"])
        if step["promotion"]:
            await promote(step)
        else:
            await transfer(step, hashes[(step["source"], step["file"]["path"])], job)
        if await digest(step["node"], step["path"]) != hashes[(step["source"], step["file"]["path"])]:
            raise ValueError(f"The installed file failed SHA-256 verification: {step['path']}.")
    return prepared["settings"]
