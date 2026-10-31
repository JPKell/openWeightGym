"""Install and remove pinned audio.cpp packages on compute, from the release's own model specs.

A package is brought down file by file into a staging folder, every file checked (SHA-256
for large files, the git blob hash for small ones), and only then moved into the `audio`
place. Its source revision and hashes are written beside it.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

from loadcoach import hub
from loadcoach.files import SPARE, room
from soundroom import model

PACKAGE = re.compile(r"[a-z0-9][a-z0-9_]{0,95}")


def git_blob(path):
    """The hash Hugging Face gives a small file that is not kept in LFS."""
    data = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def specs(manifest):
    """Offered families with their pinned upstream packages."""
    found = {}
    folder = model.specs_folder(manifest)
    for path in sorted(folder.glob('*.json')) if folder and folder.is_dir() else []:
        family = path.stem
        if not model.FAMILY.fullmatch(family):
            continue
        try:
            found[family] = model.spec(manifest, family)
        except ValueError:
            continue
    return found


def package_of(spec_value, identifier):
    """One package of a family's spec, with its download source filled in."""
    for package in spec_value.get("packages", []):
        if package["id"] == identifier:
            source = {**spec_value.get("package_defaults", {}).get("download", {}), **package.get("download", {})}
            return {**package, "repo": source.get("repo", ""), "gated": source.get("gated", False)}
    return None


def local(package, root):
    """Where each file of a package is kept, relative to the audio place."""
    prefix = package.get("strip_prefix", "")
    target = Path(package.get("target_directory", package["id"]))
    result = {}
    for remote in package.get("files", []):
        relative = remote[len(prefix):].lstrip("/") if prefix and remote.startswith(prefix) else remote
        if ".." in Path(relative).parts or Path(relative).is_absolute():
            raise ValueError("Package " + package["id"] + " names a file outside its folder.")
        result[remote] = Path(root) / target / relative
    return result


class Packages:
    """The audio place's packages, and the downloads under way."""

    def __init__(self, places, web, hub_token, log, manifest, files_in_use):
        """Keep the places, clients and the record of downloads under way."""
        self.places, self.web, self.hub_token, self.log, self.manifest = places, web, hub_token, log, manifest
        self.files_in_use = files_in_use  # every file a configured model uses, so it is never removed under one
        self.progress = {}
        self.tasks = set()

    def root(self):
        """The audio place's folder."""
        root = self.places().get("audio")
        if not root:
            raise ValueError("There is no audio place; set models_folder or [places] audio in loadcoach.toml.")
        return Path(root)

    def record_path(self, package):
        """Where a package's installation record is kept."""
        return self.root() / package.get("target_directory", package["id"]) / (".suite-package-" + package["id"] + ".json")

    def catalogue(self, include_packages=True):
        """Every offered family's packages, which are here, and how a download is going."""
        manifest = self.manifest()
        families = []
        try:
            root = self.root()
        except ValueError:
            root = None
        for family, found in specs(manifest).items():
            offered = model.CATALOG.get(family, {})
            kinds = model.family_kinds(found, family)
            if not kinds:
                continue
            packages = []
            for package in found.get("packages", []) if include_packages else []:
                package = package_of(found, package["id"])
                record = self.record_path(package) if root else None
                installed = json.loads(record.read_text()) if record and record.is_file() else None
                files = local(package, root) if root else {}
                packages.append({"id": package["id"], "name": package.get("display_name", package["id"]), "precision": package.get("precision", ""),
                                 "format": package.get("format", ""), "default": bool(package.get("default")), "repo": package["repo"],
                                 "gated": package["gated"], "installed": installed, "progress": self.progress.get(package["id"]),
                                 "folder": str(root / package.get("target_directory", package["id"])) if root else "",
                                 "files": {remote: str(path) for remote, path in files.items()}})
            families.append({"family": family, "name": found.get("display_name", family), "description": found.get("description", ""),
                             "options": {"request": found.get("options", {}).get("request", [])},
                             "status": found.get("status", ""), "kinds": kinds, "licence": offered.get("licence", "See model card"),
                             "commercial": offered.get("commercial", "unknown"), "note": offered.get("note", ""), "tasks": found.get("tasks", []), "packages": packages})
        return {"families": families, "place": str(root) if root else None, "available": bool(manifest)}

    def start(self, family, identifier, by, caller):
        """Begin bringing a package down; the catalogue shows how far it has got."""
        found = model.spec(self.manifest(), family) if isinstance(family,str) and model.FAMILY.fullmatch(family) else None
        package = package_of(found, identifier) if found and PACKAGE.fullmatch(str(identifier)) else None
        if not package or not package["repo"] or not hub.REPOSITORY.fullmatch(package["repo"]):
            raise ValueError("There is no such package in this runtime's specifications.")
        if self.progress.get(identifier, {}).get("state") in ("coming", "checking"):
            raise ValueError("That package is already coming down.")
        if self.record_path(package).is_file():
            raise ValueError("That package is already installed.")
        self.progress[identifier] = {"state": "coming", "done": 0, "of": None, "since": time.time()}
        task = asyncio.create_task(self.install(package, by, caller))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {"package": identifier, "state": "coming"}

    async def install(self, package, by, caller):
        """Bring every file down and check it, then move the whole package into place."""
        progress = self.progress[package["id"]]
        root = self.root()
        staging = root / (".staging-" + package["id"])
        try:
            token = self.hub_token()
            revision = (await hub.asked(self.web, f"/api/models/{package['repo']}/revision/main", token)).get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("Hugging Face did not name the repository's current revision.")
            folders = sorted({str(Path(remote).parent) for remote in package["files"]})
            listed = {}
            for folder in folders:
                path = f"/api/models/{package['repo']}/tree/{revision}" + ("" if folder == "." else "/" + folder)
                listed.update({one["path"]: one for one in await hub.asked(self.web, path, token, recursive="true") if one.get("type") == "file"})
            entries = []
            for remote in package["files"]:
                if remote not in listed:
                    raise ValueError(f"{package['repo']} has no file {remote} at {revision[:12]}.")
                one = listed[remote]
                entries.append({"path": remote, "size": one["size"], "sha256": (one.get("lfs") or {}).get("oid"), "oid": one.get("oid")})
            needed = sum(entry["size"] for entry in entries)
            if room(str(root))["free"] < needed + SPARE:
                raise ValueError(f"The audio place needs {needed / 2**30:.1f} GB and 1 GB to spare.")
            progress.update(of=needed)
            destinations, placed = local(package, staging), local(package, root)
            for entry in entries:
                here = placed[entry["path"]]
                if entry["sha256"] and here.is_file() and here.stat().st_size == entry["size"] and await asyncio.to_thread(hub.sha256_of, here) == entry["sha256"]:
                    destinations[entry["path"]] = here  # already in place and verified: adopted, not brought down again
                    progress["done"] += entry["size"]
                    continue
                target = destinations[entry["path"]]
                target.parent.mkdir(parents=True, exist_ok=True)
                part = {}
                base = progress["done"]
                watcher = asyncio.create_task(self.follow(part, progress, base))
                try:
                    await hub.fetch(self.web, package["repo"], entry, target, token, part)
                finally:
                    watcher.cancel()
                if not entry["sha256"] and entry["oid"] and await asyncio.to_thread(git_blob, target) != entry["oid"]:
                    raise OSError(f"{entry['path']} differs from the file Hugging Face describes.")
                progress["done"] = base + entry["size"]
            progress["state"] = "checking"
            hashes = {}
            for entry in entries:
                hashes[entry["path"]] = entry["sha256"] or await asyncio.to_thread(hub.sha256_of, destinations[entry["path"]])
            for remote, source in destinations.items():
                if source == placed[remote]:
                    continue
                placed[remote].parent.mkdir(parents=True, exist_ok=True)
                if placed[remote].exists() and await asyncio.to_thread(hub.sha256_of, placed[remote]) != hashes[remote]:
                    raise OSError(f"{placed[remote]} already exists with different content; it was left as it is.")
                source.replace(placed[remote])
            record = {"package": package["id"], "repo": package["repo"], "revision": revision, "installed": time.time(), "by": by,
                      "files": {remote: {"path": str(placed[remote]), "sha256": hashes[remote], "size": entry["size"]}
                                for entry, remote in zip(entries, package["files"])}}
            self.record_path(package).write_text(json.dumps(record, indent=2) + "\n")
            self.log.add("file", "audio-package-installed", by, caller, package=package["id"], repository=package["repo"], revision=revision, size=needed)
            self.progress.pop(package["id"], None)
        except asyncio.CancelledError:
            self.progress.pop(package["id"], None)
            raise
        except Exception as problem:
            why = str(problem) if isinstance(problem, (hub.Refused, OSError, ValueError)) else f"{type(problem).__name__}: {problem}"
            progress.update(state="failed", why=why)
            self.log.add("file", "audio-package-failed", by, caller, package=package["id"], why=why)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    async def follow(part, progress, base):
        """Mirror one file's progress into the package's."""
        while True:
            progress["done"] = base + part.get("done", 0)
            await asyncio.sleep(.5)

    def remove(self, family, identifier, by, caller):
        """Delete an installed package's files unless a configured model uses one of them."""
        found = model.spec(self.manifest(), family) if isinstance(family,str) and model.FAMILY.fullmatch(family) else None
        package = package_of(found, identifier) if found else None
        record_path = self.record_path(package) if package else None
        if not record_path or not record_path.is_file():
            raise ValueError("That package is not installed.")
        record = json.loads(record_path.read_text())
        used = self.files_in_use()
        paths = [Path(one["path"]) for one in record["files"].values()]
        if busy := [str(path) for path in paths if str(path) in used]:
            raise ValueError("A configured model uses " + ", ".join(busy) + "; remove that model first.")
        others = {Path(one["path"]) for other in record_path.parent.glob(".suite-package-*.json") if other != record_path
                  for one in json.loads(other.read_text())["files"].values()}
        for path in paths:
            if path not in others:
                path.unlink(missing_ok=True)
        record_path.unlink()
        self.log.add("file", "audio-package-removed", by, caller, package=identifier)
        return {"removed": identifier}


def routes(route, lacks, read_json, error, packages, address):
    """LoadCoach routes for the audio package catalogue."""
    async def listing(request, user):
        """The catalogue for model managers."""
        if denied := lacks(user, "manage-models"):
            return denied
        from starlette.responses import JSONResponse
        return JSONResponse(packages.catalogue(include_packages=request.url.path.endswith('/packages')))

    async def change(request, user):
        """Install or remove one package for model managers."""
        if denied := lacks(user, "manage-models"):
            return denied
        asked = await read_json(request, {"action", "family", "package"})
        if not isinstance(asked, dict):
            return asked
        from starlette.responses import JSONResponse
        try:
            if asked.get("action") == "install":
                return JSONResponse(packages.start(asked.get("family"), asked.get("package"), user.name, address(request)), status_code=202)
            if asked.get("action") == "remove":
                return JSONResponse(packages.remove(asked.get("family"), asked.get("package"), user.name, address(request)))
            return error(400, "action is install or remove.")
        except ValueError as problem:
            return error(400, str(problem))
    return [route('/loadcoach/audio/families', listing), route("/loadcoach/audio/packages", listing), route("/loadcoach/audio/packages", change, "POST")]


def main():
    """Install, list or remove packages from the command line on compute, as the Audio models page does."""
    import argparse
    import httpx
    parser = argparse.ArgumentParser(description="SoundRoom audio packages, from the pinned audio.cpp release's specs.")
    parser.add_argument("action", choices=("list", "install", "remove"))
    parser.add_argument("packages", nargs="*", help="FAMILY/PACKAGE, such as ace_step/ace_step_turbo_bf16")
    parser.add_argument("--place", required=True, help="the audio folder, such as MODELS_FOLDER/audio")
    parser.add_argument("--manifest", default=str(Path.home() / ".local/share/suite/audiocpp/current/installation.json"))
    parser.add_argument("--token-file", default="", help="a Hugging Face token file, for gated repositories")
    args = parser.parse_args()

    class Log:
        """Print what the service would log."""
        def add(self, kind, what, *who, **facts):
            """Print one event."""
            print(what, json.dumps(facts), flush=True)

    async def run():
        """Do the action and follow downloads to their end."""
        async with httpx.AsyncClient(timeout=60) as web:
            token = Path(args.token_file).read_text().strip() if args.token_file else ""
            packages = Packages(lambda: {"audio": args.place}, web, lambda: token, Log(), lambda: args.manifest, lambda: set())
            if args.action == "list":
                for family in packages.catalogue()["families"]:
                    for one in family["packages"]:
                        print(f"{family['family']}/{one['id']}", "installed" if one["installed"] else "", "recommended" if one["default"] else "")
                return
            for name in args.packages:
                family, _, identifier = name.partition("/")
                print(packages.remove(family, identifier, "command line", "local") if args.action == "remove"
                      else packages.start(family, identifier, "command line", "local"))
            while packages.tasks:
                await asyncio.sleep(5)
                for name, progress in packages.progress.items():
                    print(name, progress.get("state"), f"{progress.get('done', 0) / 2**30:.2f}/{(progress.get('of') or 0) / 2**30:.2f} GB", progress.get("why", ""), flush=True)
            failed = [name for name, progress in packages.progress.items() if progress.get("state") == "failed"]
            if failed:
                raise SystemExit("Not installed: " + ", ".join(failed))
    asyncio.run(run())


if __name__ == "__main__":
    main()
