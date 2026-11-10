"""Storage keeps every model file; the cache and each node hold copies, changed in bulk on Files' Storage view.

Also: cache and storage may be one folder, and then each file in it is in both at once.
"""

import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import spotter.app as spotter
from fake_hub import FakeHub
from front_helpers import Later, door, loadcoach_through, machine_key, turnstile_to
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config, model_places, shared_places
from loadcoach.files import SPARE
from loadcoach.groups import Group
from loadcoach.models import Models
from loadcoach.users import Users
from test_weightroom import GROUPS, Switch, desk, gguf, sign_in  # noqa: F401
from turnstile import library
from weightroom.app import Settings, build_app


# ---- what the page offers, worked out from a listing ------------------------------------------

def listing(**nodes):
    """A file inventory as turnstile gives it: front as the store, with a cache and storage, and `nodes`."""
    front_places = [{"name": "llm", "folder": "/f/cache/llm", "tier": "cache", "free": 100 * SPARE, "filesystem": "c"},
                    {"name": "storage_llm", "folder": "/f/storage/llm", "tier": "storage", "free": 100 * SPARE, "filesystem": "s"}]
    made = [{"name": "front", "answering": True, "places": front_places, "files": nodes.pop("front", [])}]
    for name, files in nodes.items():
        made.append({"name": name, "answering": files is not None, "files": files or [],
                     "places": [{"name": "llm", "folder": f"/{name}/llm", "tier": "cache", "free": 3 * SPARE, "filesystem": name}]
                     if files is not None else []})
    return {"artifact_machine": "front", "nodes": made, "distributions": []}


def file(path, size=10, **more):
    """One file of a listing, in the place its folder says."""
    place = "storage_llm" if "/storage/" in path else "llm"
    return {"path": path, "size": size, "place": place, "tier": "storage" if place.startswith("storage_") else "cache",
            "facts": {"what": "model", "name": Path(path).stem}, **more}


def test_rows_list_storage_first_then_files_found_only_elsewhere():
    found = listing(front=[file("/f/storage/llm/a.gguf"), file("/f/cache/llm/a.gguf"), file("/f/cache/llm/loose.gguf")],
                    rig=[file("/rig/llm/a.gguf", used_by=["chat"]), file("/rig/llm/own.gguf")], away=None)
    assert [column["name"] for column in library.columns(found)] == ["storage", "cache", "rig", "away"]
    rows = {row["name"]: row for row in library.rows(found)}
    assert list(rows) == ["a.gguf", "loose.gguf", "own.gguf"]
    assert rows["a.gguf"]["stored"] and not rows["loose.gguf"]["stored"] and not rows["own.gguf"]["stored"]
    columns = {column["name"]: column for column in library.columns(found)}
    box = lambda name, at: library.cell(rows[name], columns[at], found)
    assert box("a.gguf", "storage") == {"present": True, "why": "In storage. Delete it from storage on the file's own page.", "used": []}
    assert box("a.gguf", "cache") == {"present": True, "why": "", "used": []}
    assert box("a.gguf", "rig")["why"] == "Used by chat. Change that model first."
    assert box("a.gguf", "away")["why"] == "away is not answering."
    assert box("own.gguf", "rig")["why"].startswith("Not in storage, so this may be the only copy.")
    assert box("own.gguf", "storage") == {"present": False, "why": "", "used": []}


def test_a_store_file_a_node_on_the_same_machine_loads_is_kept():
    shared_path = "/f/cache/llm/a.gguf"
    found = listing(front=[file("/f/storage/llm/a.gguf"), file(shared_path)], cpu=[file(shared_path, used_by=["local"])])
    row, = library.rows(found)
    cache = next(column for column in library.columns(found) if column["name"] == "cache")
    assert library.cell(row, cache, found)["why"] == "Used by local. Change that model first."


def test_plan_removes_first_copies_from_a_kept_copy_and_skips_what_is_already_so():
    found = listing(front=[file("/f/storage/llm/a.gguf"), file("/f/cache/llm/a.gguf"), file("/f/storage/llm/b.gguf")],
                    rig=[file("/rig/llm/b.gguf")])
    rows = {row["name"]: row["id"] for row in library.rows(found)}
    steps = library.plan(found, [
        {"row": rows["a.gguf"], "at": "rig", "keep": True},
        {"row": rows["a.gguf"], "at": "cache", "keep": False},  # the cache copy goes, so rig's comes from storage
        {"row": rows["b.gguf"], "at": "rig", "keep": False},
        {"row": rows["b.gguf"], "at": "storage", "keep": True},  # already so
    ])
    assert [(step["action"], step["file"], step["at"]) for step in steps] == [
        ("remove", "a.gguf", "cache"), ("remove", "b.gguf", "rig"), ("copy", "a.gguf", "rig")]
    copy = steps[-1]
    assert copy["source"] == "front" and copy["source_file"]["path"] == "/f/storage/llm/a.gguf"
    assert copy["place"]["name"] == "llm" and copy["path"] == "/rig/llm/a.gguf" and copy["relative"] == "a.gguf"
    assert steps[1]["paths"] == ["/rig/llm/b.gguf"]


def test_plan_refuses_every_change_it_cannot_make_and_then_makes_none():
    found = listing(front=[file("/f/storage/llm/a.gguf"), file("/f/storage/llm/big.gguf", size=5 * SPARE)],
                    rig=[file("/rig/llm/a.gguf", used_by=["chat"]), file("/rig/llm/own.gguf")])
    rows = {row["name"]: row["id"] for row in library.rows(found)}
    with pytest.raises(ValueError) as refused:
        library.plan(found, [{"row": rows["a.gguf"], "at": "rig", "keep": False},
                             {"row": rows["own.gguf"], "at": "rig", "keep": False},
                             {"row": rows["a.gguf"], "at": "storage", "keep": False},
                             {"row": rows["big.gguf"], "at": "rig", "keep": True},
                             {"row": "gone", "at": "rig", "keep": True}])
    said = str(refused.value)
    for why in ("a.gguf at rig: Used by chat.", "own.gguf at rig: Not in storage", "a.gguf at Storage: In storage.",
                "rig has 3.0 GiB free; these copies need 5.0 GiB plus 1 GiB to spare.", "Refresh the page", "No files were changed."):
        assert why in said


def test_room_freed_on_a_node_counts_toward_the_copies_made_there():
    big = int(2.5 * SPARE)
    found = listing(front=[file("/f/storage/llm/old.gguf", size=big), file("/f/storage/llm/new.gguf", size=big)],
                    rig=[file("/rig/llm/old.gguf", size=big)])
    rows = {row["name"]: row["id"] for row in library.rows(found)}
    swap = [{"row": rows["new.gguf"], "at": "rig", "keep": True}]
    with pytest.raises(ValueError, match="rig has 3.0 GiB free"):
        library.plan(found, swap)
    steps = library.plan(found, swap + [{"row": rows["old.gguf"], "at": "rig", "keep": False}])
    assert [step["action"] for step in steps] == ["remove", "copy"]


def test_one_folder_for_cache_and_storage_is_shared_and_its_cache_box_is_its_storage_copy():
    places = model_places("/one", {}, "/one")
    assert shared_places(places) == set(places) and places["llm"] == places["storage_llm"]
    assert not shared_places(model_places("/cache", {}, "/storage"))
    found = listing(front=[file("/f/cache/llm/a.gguf", shared=True)])
    for place in found["nodes"][0]["places"]:
        place.update(folder="/f/one/llm", shared=True)
    assert [column["name"] for column in library.columns(found)] == ["storage"]  # no cache of its own to tick
    row, = library.rows(found)
    assert row["stored"] and set(row["copies"]) == {"storage", "cache"}


# ---- the whole way round: a page, a bulk change, and the files on front and a node -------------

@pytest.fixture
def library_desk(tmp_path, monkeypatch):
    """weightroom and turnstile with front cache and storage, and a compute node whose spotter receives files.

    Yields the browser, the switch to the node, front's cache and storage, and the node's cache.
    """
    monkeypatch.setattr("weightroom.app.LOOK_EVERY", 0.05)
    (tmp_path / "data").mkdir()
    users = Users(str(tmp_path / "data" / "accounts.db"), GROUPS)
    for name, group in (("jordan", "admin"), ("panel", "front")):
        users.add(name, group)
        if name != "panel":
            users.change(name, password="correct horse")
    key, database, node = machine_key(tmp_path), str(tmp_path / "loadcoach.db"), tmp_path / "node"
    places = model_places(str(node), {})
    for folder in places.values():
        Path(folder).mkdir(parents=True)
    later = Later()
    compute = Switch(build_loadcoach(Config(database=database, gpus=(), places=places, nvidia_smi="no-such-program",
                                            machine_key_file=key, front="http://front:8442"), FakeHub({}).client(), door(later, 8442)))
    receiver = Switch(spotter.build_app(spotter.Settings(services={}, machine_key_file=key, database=database, places=places,
                                                         history=str(tmp_path / "spotter.db"), disks=(str(tmp_path),))))
    front = later.app = turnstile_to(tmp_path, users, {8442: compute, 8444: receiver}, front_settings=True)
    compute.front = front
    settings = Settings(compute="http://compute", key=users.new_key("panel"), data=str(tmp_path / "data"))
    with TestClient(build_app(settings, door(front, 8442))) as browser:
        sign_in(browser, "jordan")
        cache, storage = tmp_path / "cache", tmp_path / "storage"
        saved = browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(cache),
                                                            "models_storage_folder": str(storage)})
        assert "Front file locations are available now" in saved.text
        yield browser, compute, cache, storage, node


def boxes(page):
    """The Storage view's boxes by their names, each with what it sends and whether it is ticked or locked."""
    found = re.findall(r'name="want" value="([^"]+)" data-column="\d+" aria-label="([^"]+)"( checked)?( disabled)?>', page)
    return {label.split(": ", 1)[0]: {"value": value, "label": label, "ticked": bool(ticked), "locked": bool(locked)}
            for value, label, ticked, locked in found}


def send(browser, page, tick=(), clear=(), forged=()):
    """Send the Storage view's form as a browser would, after ticking and clearing the boxes named."""
    shown = boxes(page)
    wanted = {name for name, box in shown.items() if box["ticked"] and not box["locked"]} | set(tick)
    wanted -= set(clear)
    form = {"do": "arrange", "column": re.findall(r'name="column" value="([^"]+)"', page),
            "boxes": re.findall(r'name="boxes" value="([^"]+)"', page) + list(forged),
            "want": [shown[name]["value"] for name in wanted]}
    return browser.post("/files", data=form)


def finished(browser, seconds=10):
    """Wait for the bulk change to end, and give the Storage view as it is then."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page = browser.get("/files?tier=storage&complete=1").text
        if "Bulk change finished" in page:
            return page
        time.sleep(.05)
    raise AssertionError("The bulk change did not finish: " + page[page.find("files-distributions"):][:800])


def test_bulk_change_copies_to_cache_and_node_keeps_a_node_file_in_storage_and_removes_a_copy(library_desk):
    browser, compute, cache, storage, node = library_desk
    a = Path(gguf(storage / "llm" / "a.gguf", general__name="A", general__architecture="tiny"))
    used = Path(gguf(storage / "llm" / "b.gguf", general__name="B", general__architecture="tiny"))
    (node / "llm" / "b.gguf").write_bytes(used.read_bytes())
    d = Path(gguf(storage / "llm" / "d.gguf", general__name="D", general__architecture="tiny"))
    (node / "llm" / "d.gguf").write_bytes(d.read_bytes())
    own = Path(gguf(node / "llm" / "own.gguf", general__name="Own", general__architecture="tiny"))
    Models(str(node.parent / "loadcoach.db")).put("mine", {"gpus": 0, "flags": {"model": str(node / "llm" / "b.gguf"), "ctx-size": 2048}})
    browser.post("/files", data={"do": "refresh-files", "back": "/files?tier=storage"})

    page = browser.get("/files?tier=storage&complete=1").text
    head = page.split('id="files-storage-table"', 1)[1].split("</thead>", 1)[0]
    assert all(f">{label}<input" in head for label in ("Storage", "Cache", "compute"))
    shown = boxes(page)
    assert shown["a.gguf in Storage"] == {**shown["a.gguf in Storage"], "ticked": True, "locked": True}
    assert not shown["a.gguf in Cache"]["ticked"] and not shown["a.gguf in compute"]["locked"]
    assert shown["b.gguf in compute"]["label"] == "b.gguf in compute: Used by mine. Change that model first."
    assert shown["d.gguf in compute"] == {**shown["d.gguf in compute"], "ticked": True, "locked": False}
    assert shown["own.gguf in compute"]["locked"] and not shown["own.gguf in Storage"]["locked"]
    assert page.count("not in storage</span>") == 1 and "in use</small>" in page

    started = send(browser, page, tick=["a.gguf in Cache", "a.gguf in compute", "own.gguf in Storage"], clear=["d.gguf in compute"])
    assert started.status_code == 200 and "Started 3 copies and 1 removal." in started.text, started.text[started.text.find("said-box"):][:400]
    page = finished(browser)
    assert "Bulk change finished: 4 of 4 done." in page and "failed" not in page.split("files-distributions", 1)[1][:600]
    assert (cache / "llm" / "a.gguf").read_bytes() == a.read_bytes() == (node / "llm" / "a.gguf").read_bytes()
    assert (storage / "llm" / "own.gguf").read_bytes() == own.read_bytes() and own.is_file()
    assert not (node / "llm" / "d.gguf").exists() and d.is_file()  # the storage copy stays
    assert not list((storage / "llm").glob(".*.receiving"))
    browser.post("/files", data={"do": "refresh-files", "back": "/files?tier=storage"})
    shown = boxes(browser.get("/files?tier=storage&complete=1").text)
    assert shown["a.gguf in Cache"]["ticked"] and shown["a.gguf in compute"]["ticked"] and not shown["d.gguf in compute"]["ticked"]
    assert shown["own.gguf in Storage"]["locked"] and not shown["own.gguf in compute"]["locked"]  # kept in storage, so removable


def test_a_refused_change_leaves_every_file_as_it_was(library_desk):
    browser, compute, cache, storage, node = library_desk
    gguf(storage / "llm" / "a.gguf", general__name="A", general__architecture="tiny")
    used = Path(gguf(storage / "llm" / "b.gguf", general__name="B", general__architecture="tiny"))
    (node / "llm" / "b.gguf").write_bytes(used.read_bytes())
    Models(str(node.parent / "loadcoach.db")).put("mine", {"gpus": 0, "flags": {"model": str(node / "llm" / "b.gguf"), "ctx-size": 2048}})
    browser.post("/files", data={"do": "refresh-files", "back": "/files?tier=storage"})
    page = browser.get("/files?tier=storage&complete=1").text
    b = boxes(page)["b.gguf in compute"]["value"].split(":")[0]
    # A page that offered to clear the locked box, as a stale or forged one might: turnstile checks again.
    forged = send(browser, page, tick=["a.gguf in compute"], forged=[f"{b}:--1"])
    assert "b.gguf at compute: Used by mine. Change that model first. No files were changed." in forged.text
    assert 'class="alert alert-danger said said-box"' in forged.text
    assert not (node / "llm" / "a.gguf").exists() and (node / "llm" / "b.gguf").exists()
    assert "Nothing was changed" in send(browser, page).text


def test_one_folder_for_cache_and_storage(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, "jordan")
    one = tmp_path / "models"
    saved = browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(one), "models_storage_folder": str(one)})
    assert saved.status_code == 200 and "Front file locations are available now" in saved.text
    model = Path(gguf(one / "llm" / "both.gguf", general__name="Both", general__architecture="tiny"))
    browser.post("/files", data={"do": "refresh-files", "back": "/files?tier=storage"})

    page = browser.get("/files?tier=storage&complete=1").text
    places = page.split('id="files-places"', 1)[1].split('<nav class="nav nav-pills', 1)[0]
    assert 'aria-label="Storage and cache"' in places and 'aria-label="Front cache"' not in places and 'aria-label="Storage"' not in places
    shown = boxes(page)
    assert shown["both.gguf in Storage"]["ticked"] and not any(" in Cache" in name for name in shown)  # listed once, no cache of its own
    assert browser.get("/files?tier=cache&complete=1").text.count("both.gguf</a>") == 1
    detail = browser.get("/file", params={"path": str(model)}).text
    assert 'value="storage_llm"' not in detail  # the same folder is not somewhere else to go

    store, owner = compute.front.state.front_files, compute.front.state.users.by_name("jordan")
    cleared = browser.portal.call(store.request, owner, "POST", "/loadcoach/files/cleanup", b'{"mode": "clear-cache"}')
    assert cleared.status_code == 400 and "one folder" in cleared.json()["error"]["message"]
    assert model.is_file()


def test_a_node_with_one_folder_keeps_unused_files_through_cleanup(tmp_path):
    one = tmp_path / "models"
    places = model_places(str(one), {}, str(one))
    for folder in places.values():
        Path(folder).mkdir(parents=True, exist_ok=True)
    model = Path(gguf(one / "llm" / "unused.gguf", general__architecture="tiny"))
    users = Users(str(tmp_path / "accounts.db"), {"manager": Group(can=frozenset({"manage-models"}))})
    users.add("manager", "manager")
    headers = {"authorization": "Bearer " + users.new_key("manager")}
    with loadcoach_through(tmp_path, users, places=places, gpus=()) as front:
        listed = front.get("/loadcoach/files", headers=headers).json()
        assert [file["place"] for file in listed["files"]] == ["llm"] and listed["files"][0]["shared"]
        assert all(place["shared"] for place in listed["places"] if place["name"] in ("llm", "storage_llm"))
        started = front.post("/loadcoach/files/cleanup", headers=headers, json={"mode": "unused"})
        assert started.status_code == 202
        deadline = time.monotonic() + 5
        while front.get("/loadcoach/files", headers=headers).json()["cleanup"].get("state") == "running" and time.monotonic() < deadline:
            time.sleep(.05)
    assert model.is_file()
