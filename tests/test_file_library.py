"""Storage keeps every model file; each other node holds copies, changed in bulk on Files' Storage view.

There is one kind of model folder: a node's model storage, which its models load from. A retired
second root (models_storage_folder) only adds ordinary places.
"""

import re
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import spotter.app as spotter
from fake_hub import FakeHub
from front_helpers import Later, door, loadcoach_through, machine_key, turnstile_to
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config, model_places
from loadcoach.files import SPARE
from loadcoach.groups import Group
from loadcoach.models import Models
from loadcoach.users import Users
from test_weightroom import GROUPS, Switch, desk, gguf, sign_in  # noqa: F401
from turnstile import library
from weightroom.app import Settings, build_app


# ---- what the page offers, worked out from a listing ------------------------------------------

def listing(**nodes):
    """A file inventory as turnstile gives it: control node as the store, with its model storage, and `nodes`."""
    front_places = [{"name": "llm", "folder": "/f/storage/llm", "free": 100 * SPARE, "filesystem": "s"}]
    made = [{"name": "front", "answering": True, "places": front_places, "files": nodes.pop("front", [])}]
    for name, files in nodes.items():
        made.append({"name": name, "answering": files is not None, "files": files or [],
                     "places": [{"name": "llm", "folder": f"/{name}/llm", "free": 3 * SPARE, "filesystem": name}]
                     if files is not None else []})
    return {"artifact_machine": "front", "nodes": made, "distributions": []}


def file(path, size=10, **more):
    """One file of a listing, in its node's llm folder."""
    return {"path": path, "size": size, "place": "llm", "facts": {"what": "model", "name": Path(path).stem}, **more}


def test_rows_list_storage_first_then_files_found_only_elsewhere():
    found = listing(front=[file("/f/storage/llm/a.gguf"), file("/f/storage/llm/loose.gguf")],
                    rig=[file("/rig/llm/a.gguf", used_by=["chat"]), file("/rig/llm/own.gguf"), file("/rig/llm/loose.gguf")], away=None)
    assert [column["name"] for column in library.columns(found)] == ["storage", "rig", "away"]  # no cache column
    rows = {row["name"]: row for row in library.rows(found)}
    assert list(rows) == ["a.gguf", "loose.gguf", "own.gguf"]
    assert rows["a.gguf"]["stored"] and rows["loose.gguf"]["stored"] and not rows["own.gguf"]["stored"]
    columns = {column["name"]: column for column in library.columns(found)}
    box = lambda name, at: library.cell(rows[name], columns[at], found)
    assert box("a.gguf", "storage") == {"present": True, "why": "In storage. Delete it from storage on the file's own page.", "used": []}
    assert box("loose.gguf", "rig") == {"present": True, "why": "", "used": []}
    assert box("a.gguf", "rig")["why"] == "Used by chat. Change that model first."
    assert box("a.gguf", "away")["why"] == "away is not answering."
    assert box("own.gguf", "rig")["why"].startswith("Not in storage, so this may be the only copy.")
    assert box("own.gguf", "storage") == {"present": False, "why": "", "used": []}


def test_plan_removes_first_copies_from_a_kept_copy_and_skips_what_is_already_so():
    found = listing(front=[file("/f/storage/llm/a.gguf"), file("/f/storage/llm/b.gguf")],
                    rig=[file("/rig/llm/b.gguf")], pod=[file("/pod/llm/a.gguf")])
    rows = {row["name"]: row["id"] for row in library.rows(found)}
    steps = library.plan(found, [
        {"row": rows["a.gguf"], "at": "rig", "keep": True},
        {"row": rows["a.gguf"], "at": "pod", "keep": False},  # pod's copy goes, so rig's comes from storage
        {"row": rows["b.gguf"], "at": "rig", "keep": False},
        {"row": rows["b.gguf"], "at": "storage", "keep": True},  # already so
    ])
    assert [(step["action"], step["file"], step["at"]) for step in steps] == [
        ("remove", "a.gguf", "pod"), ("remove", "b.gguf", "rig"), ("copy", "a.gguf", "rig")]
    copy = steps[-1]
    assert copy["source"] == "front" and copy["source_file"] == {"path": "/f/storage/llm/a.gguf", "place": "llm", "size": 10}
    assert copy["place"] == {"name": "llm", "folder": "/rig/llm"} and copy["path"] == "/rig/llm/a.gguf" and copy["relative"] == "a.gguf"
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


def test_one_model_folder_per_node_and_a_retired_second_root_adds_ordinary_places():
    one = model_places("/one", {})
    assert set(one) == {"llm", "adapters", "projectors", "image", "audio", "encoders", "classifier"}
    assert model_places("/one", {}, "/one") == one  # the same folder twice is one set of places
    both = model_places("/one", {}, "/two")
    assert both["llm"] == "/one/llm" and both["storage_llm"] == "/two/llm"
    assert model_places("", {}, "/two")["storage_llm"] == "/two/llm"
    # A node with only the retired root still takes copies, into the folder of the same kind.
    found = listing(front=[file("/f/storage/llm/a.gguf")], rig=[])
    found["nodes"][1]["places"] = [{"name": "storage_llm", "folder": "/rig/llm", "free": 3 * SPARE, "filesystem": "rig"}]
    row, = library.rows(found)
    step, = library.plan(found, [{"row": row["id"], "at": "rig", "keep": True}])
    assert step["place"]["name"] == "storage_llm" and step["path"] == "/rig/llm/a.gguf"


# ---- the whole way round: a page, a bulk change, and the files on the control node and a node -------------

@pytest.fixture
def library_desk(tmp_path, monkeypatch):
    """weightroom and turnstile with control node model storage, and a compute node whose spotter receives files.

    Yields the browser, the switch to the node, the control node's storage, and the node's storage.
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
    front = later.app = turnstile_to(tmp_path, users, {8442: compute, 8444: receiver}, keep_console_settings_on_front=True)
    compute.front = front
    settings = Settings(compute="http://compute", key=users.new_key("panel"), data=str(tmp_path / "data"))
    with TestClient(build_app(settings, door(front, 8442))) as browser:
        sign_in(browser, "jordan")
        storage = tmp_path / "storage"
        saved = browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(storage)})
        assert "Control node file locations are available now" in saved.text
        yield browser, compute, storage, node


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
        page = browser.get("/files?complete=1").text
        if "Bulk change finished" in page:
            return page
        time.sleep(.05)
    raise AssertionError("The bulk change did not finish: " + page[page.find("files-distributions"):][:800])


def test_bulk_change_copies_to_a_node_keeps_a_node_file_in_storage_and_removes_a_copy(library_desk, monkeypatch):
    browser, compute, storage, node = library_desk
    a = Path(gguf(storage / "llm" / "a.gguf", general__name="A", general__architecture="tiny"))
    used = Path(gguf(storage / "llm" / "b.gguf", general__name="B", general__architecture="tiny"))
    (node / "llm" / "b.gguf").write_bytes(used.read_bytes())
    d = Path(gguf(storage / "llm" / "d.gguf", general__name="D", general__architecture="tiny"))
    (node / "llm" / "d.gguf").write_bytes(d.read_bytes())
    own = Path(gguf(node / "llm" / "own.gguf", general__name="Own", general__architecture="tiny"))
    Models(str(node.parent / "loadcoach.db")).put("mine", {"gpus": 0, "flags": {"model": str(node / "llm" / "b.gguf"), "ctx-size": 2048}})
    browser.post("/files", data={"do": "refresh-files", "back": "/files?"})

    page = browser.get("/files?complete=1").text
    head = page.split('id="files-storage-table"', 1)[1].split("</thead>", 1)[0]
    assert all(f">{label}<input" in head for label in ("Storage", "compute")) and ">Cache<" not in head
    shown = boxes(page)
    assert shown["a.gguf in Storage"] == {**shown["a.gguf in Storage"], "ticked": True, "locked": True}
    assert not shown["a.gguf in compute"]["ticked"] and not shown["a.gguf in compute"]["locked"]
    assert shown["b.gguf in compute"]["label"] == "b.gguf in compute: Used by mine. Change that model first."
    assert shown["d.gguf in compute"] == {**shown["d.gguf in compute"], "ticked": True, "locked": False}
    assert shown["own.gguf in compute"]["locked"] and not shown["own.gguf in Storage"]["locked"]
    assert page.count("not in storage</span>") == 1 and "in use</small>" in page

    started = send(browser, page, tick=["a.gguf in compute", "own.gguf in Storage"], clear=["d.gguf in compute"])
    assert started.status_code == 200 and "Started 2 copies and 1 removal." in started.text, started.text[started.text.find("said-box"):][:400]
    page = finished(browser)
    assert "Bulk change finished: 3 of 3 done." in page and "failed" not in page.split("files-distributions", 1)[1][:600]
    assert a.read_bytes() == (node / "llm" / "a.gguf").read_bytes()
    assert (storage / "llm" / "own.gguf").read_bytes() == own.read_bytes() and own.is_file()
    assert not (node / "llm" / "d.gguf").exists() and d.is_file()  # the storage copy stays
    assert not list((storage / "llm").glob(".*.receiving"))
    browser.post("/files", data={"do": "refresh-files", "back": "/files?"})
    shown = boxes(browser.get("/files?complete=1").text)
    assert shown["a.gguf in compute"]["ticked"] and not shown["d.gguf in compute"]["ticked"]
    assert shown["own.gguf in Storage"]["locked"] and not shown["own.gguf in compute"]["locked"]  # kept in storage, so removable

    # A finished task leaves the list a day after it ends, or at once with Clear finished.
    assert 'value="clear-finished"' in page
    monkeypatch.setattr("turnstile.app.FINISHED_TASK_SECONDS", 0)
    assert "Bulk change finished" not in browser.get("/files?complete=1").text
    monkeypatch.undo()
    cleared = browser.post("/files", data={"do": "clear-finished"})
    assert "Finished tasks cleared." in cleared.text
    page = browser.get("/files?complete=1").text
    assert "Bulk change finished" not in page and 'value="clear-finished"' not in page


def test_video_components_have_their_own_file_type():
    from loadcoach.platform_services import file_kind
    assert file_kind({"facts": {"what": "image", "image_identity": {"family": "wan2.2"}}}) == "video"
    assert file_kind({"facts": {"what": "image", "image_identity": {"family": "flux"}}}) == "image"
    assert file_kind({"what": "adapter", "facts": {}}) == "adapter" and file_kind({}) == "model"


def test_a_refused_change_leaves_every_file_as_it_was(library_desk):
    browser, compute, storage, node = library_desk
    gguf(storage / "llm" / "a.gguf", general__name="A", general__architecture="tiny")
    used = Path(gguf(storage / "llm" / "b.gguf", general__name="B", general__architecture="tiny"))
    (node / "llm" / "b.gguf").write_bytes(used.read_bytes())
    Models(str(node.parent / "loadcoach.db")).put("mine", {"gpus": 0, "flags": {"model": str(node / "llm" / "b.gguf"), "ctx-size": 2048}})
    browser.post("/files", data={"do": "refresh-files", "back": "/files?"})
    page = browser.get("/files?complete=1").text
    b = boxes(page)["b.gguf in compute"]["value"].split(":")[0]
    # A page that offered to clear the locked box, as a stale or forged one might: turnstile checks again.
    forged = send(browser, page, tick=["a.gguf in compute"], forged=[f"{b}:-1"])
    assert "b.gguf at compute: Used by mine. Change that model first. No files were changed." in forged.text
    assert 'class="alert alert-danger d-block said said-box"' in forged.text
    assert not (node / "llm" / "a.gguf").exists() and (node / "llm" / "b.gguf").exists()
    assert "Nothing was changed" in send(browser, page).text


def test_control_node_model_storage_is_one_folder_with_no_cache_to_clear(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, "jordan")
    one = tmp_path / "models"
    settings = browser.get("/settings?file_machine=front").text
    assert 'name="models_folder"' in settings and 'name="models_storage_folder"' not in settings  # no second folder to set
    assert ">Model storage path<" in settings and "cache" not in settings.split('id="model-files"', 1)[1].split("</form>", 2)[1].lower()
    saved = browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(one)})
    assert saved.status_code == 200 and "Control node file locations are available now" in saved.text
    model = Path(gguf(one / "llm" / "both.gguf", general__name="Both", general__architecture="tiny"))
    browser.post("/files", data={"do": "refresh-files", "back": "/files?"})

    page = browser.get("/files?complete=1").text
    places = page.split('id="files-places"', 1)[1].split('<nav class="nav nav-pills', 1)[0]
    assert 'aria-label="Storage"' in places and "cache" not in places.lower()
    assert 'value="clear-cache"' not in page and 'value="consolidate-cache"' not in page and "Optional maintenance" not in page
    assert boxes(page)["both.gguf in Storage"]["ticked"]
    detail = browser.get("/file", params={"path": str(model)}).text
    assert 'value="storage_llm"' not in detail and "active cache" not in detail

    store, owner = compute.front.state.front_files, compute.front.state.users.by_name("jordan")
    listed = browser.portal.call(store.request, owner, "GET", "/loadcoach/files", b"").json()
    assert [place["name"] for place in listed["places"]] == ["llm", "adapters", "projectors", "image", "audio", "encoders", "classifier"]
    assert all("tier" not in one and "shared" not in one for one in listed["places"] + listed["files"])
    cleared = browser.portal.call(store.request, owner, "POST", "/loadcoach/files/cleanup", b'{"mode": "clear-cache"}')
    assert cleared.status_code == 409 and model.is_file()
    assert browser.post("/files", data={"do": "clear-cache"}).status_code == 400 and model.is_file()


def test_a_former_second_folder_shows_until_it_is_cleared_and_its_files_stay_usable(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, "jordan")
    one, former = tmp_path / "models", tmp_path / "library"
    store, owner = compute.front.state.front_files, compute.front.state.users.by_name("jordan")
    compute.front.state.console_settings.changes.put("turnstile", "models_storage_folder", str(former), "jordan")  # as an older installation saved it
    saved = browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(one)})
    assert saved.status_code == 200
    kept = Path(gguf(former / "llm" / "kept.gguf", general__name="Kept", general__architecture="tiny"))
    settings = browser.get("/settings?file_machine=front").text
    assert f'name="models_storage_folder" class="form-control font-monospace" value="{former}"' in settings and "Former second model folder" in settings
    listed = browser.portal.call(store.request, owner, "GET", "/loadcoach/files?refresh=1", b"").json()
    assert {"llm", "storage_llm"} <= {place["name"] for place in listed["places"]}
    assert [file["path"] for file in listed["files"]] == [str(kept)]
    cleared = browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(one), "models_storage_folder": ""})
    assert cleared.status_code == 200
    assert 'name="models_storage_folder"' not in browser.get("/settings?file_machine=front").text
    listed = browser.portal.call(store.request, owner, "GET", "/loadcoach/files?refresh=1", b"").json()
    assert not any(place["name"].startswith("storage_") for place in listed["places"]) and kept.is_file()


def test_a_node_has_no_cleanup_that_removes_its_unused_model_files(tmp_path):
    one = tmp_path / "models"
    places = model_places(str(one), {})
    for folder in places.values():
        Path(folder).mkdir(parents=True, exist_ok=True)
    model = Path(gguf(one / "llm" / "unused.gguf", general__architecture="tiny"))
    users = Users(str(tmp_path / "accounts.db"), {"manager": Group(can=frozenset({"manage-models"}))})
    users.add("manager", "manager")
    headers = {"authorization": "Bearer " + users.new_key("manager")}
    with loadcoach_through(tmp_path, users, places=places, gpus=()) as front:
        listed = front.get("/loadcoach/files", headers=headers).json()
        assert [file["place"] for file in listed["files"]] == ["llm"] and "cleanup" not in listed
        assert all("tier" not in one and "shared" not in one for one in listed["places"] + listed["files"])
        assert front.post("/loadcoach/files/cleanup", headers=headers, json={"mode": "unused"}).status_code in (404, 405)
    assert model.is_file()


def test_transfer_plan_preserves_storage_and_requires_source_and_space_before_removal():
    found = listing(front=[file('/f/storage/llm/a.gguf')], pod=[file('/pod/llm/a.gguf')], rig=[])
    row, = library.rows(found)
    transfer = {'rows': [row['id']], 'source': 'pod', 'destination': 'rig', 'mode': 'move'}
    changes = library.transfer_changes(found, transfer)
    assert [step['action'] for step in library.plan(found, changes, copy_first=True)] == ['copy', 'remove']
    for bad in ({'source': 'storage'}, {'source': 'rig'}, {'destination': 'pod'}, {'destination': 'cache'}, {'rows': ['gone']}):
        with pytest.raises(ValueError):
            library.transfer_changes(found, {**transfer, **bad})
    # An exchange cannot claim the disk space from a deletion that must wait for the copy.
    big = int(2.5 * SPARE)
    crowded = listing(front=[file('/f/storage/llm/old.gguf', size=big), file('/f/storage/llm/new.gguf', size=big)],
                      rig=[file('/rig/llm/old.gguf', size=big)])
    rows = {row['name']: row['id'] for row in library.rows(crowded)}
    with pytest.raises(ValueError, match='rig has 3.0 GiB free'):
        library.plan(crowded, [{'row': rows['old.gguf'], 'at': 'rig', 'keep': False},
                              {'row': rows['new.gguf'], 'at': 'rig', 'keep': True}], copy_first=True)


def test_cleanup_preserves_storage_unique_and_in_use_files():
    found = listing(front=[file('/f/storage/llm/a.gguf'), file('/f/storage/llm/b.gguf'), file('/f/storage/llm/c.gguf')],
                    rig=[file('/rig/llm/a.gguf', used_by=['chat']), file('/rig/llm/b.gguf'), file('/rig/llm/unique.gguf')])
    rows = {row['name']: row['id'] for row in library.rows(found)}
    assert library.cleanup_changes(found) == [{'row': rows['b.gguf'], 'at': 'rig', 'keep': False}]  # never a storage file
    found['nodes'].append({'name': 'offline', 'answering': False, 'places': [], 'files': []})
    assert library.cleanup_changes(found) == [{'row': rows['b.gguf'], 'at': 'rig', 'keep': False}]


def test_bulk_copy_never_overwrites_a_different_file_at_the_destination(library_desk):
    browser, compute, storage, node = library_desk
    for name in ('a', 'b'):
        gguf(storage / 'llm' / f'{name}.gguf', general__name=name, general__architecture='tiny')
    conflict = Path(gguf(node / 'llm' / 'a.gguf', general__name='Different', general__architecture='tiny'))
    before = conflict.read_bytes()
    browser.post('/files', data={'do': 'refresh-files'})
    page = browser.get('/files?complete=1').text
    selected = re.findall(r'name="selected" value="([^"]+)"', page)[:2]  # the stored a and b; the node's own a.gguf is a different file
    answer = browser.post('/files', data={'do': 'transfer', 'selected': selected, 'source': 'storage', 'destination': 'compute', 'operation': 'copy'})
    assert 'Transfer started' in answer.text, str(answer.url)
    result = finished(browser)
    assert 'failed' in result.split('files-distributions', 1)[1][:1500]
    assert conflict.read_bytes() == before
    assert (node / 'llm' / 'b.gguf').read_bytes() == (storage / 'llm' / 'b.gguf').read_bytes()
    assert (storage / 'llm' / 'a.gguf').exists() and (storage / 'llm' / 'b.gguf').exists()


def test_one_button_cleanup_only_removes_safe_node_copies(library_desk):
    browser, compute, storage, node = library_desk
    original = Path(gguf(storage / 'llm' / 'spare.gguf', general__name='Spare', general__architecture='tiny'))
    (node / 'llm' / original.name).write_bytes(original.read_bytes())
    unique = Path(gguf(node / 'llm' / 'unique.gguf', general__name='Unique', general__architecture='tiny'))
    browser.post('/files', data={'do': 'refresh-files'})
    answer = browser.post('/files', data={'do': 'clean-unused'})
    assert 'Unused-copy cleanup started' in answer.text
    finished(browser)
    assert original.exists() and unique.exists()
    assert not (node / 'llm' / original.name).exists()


def test_files_matrix_browser_selection_transfer_and_polling(library_desk, tmp_path):
    from test_console_browser import browser_context, playwright
    browser, compute, storage, node = library_desk
    for name in ('alpha', 'beta'):
        gguf(storage / 'llm' / f'{name}.gguf', general__name=name.title(), general__architecture='tiny')
    browser.post('/files', data={'do': 'refresh-files'})
    with playwright.sync_playwright() as engine:
        chrome, page = browser_context(engine, browser)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/files')
        playwright.expect(page.locator('[data-file-row]')).to_have_count(2)
        playwright.expect(page.get_by_text('Every file in storage. Working copies where you need them.')).to_have_count(0)
        playwright.expect(page.locator('[data-matrix-changes]')).to_be_hidden()
        page.screenshot(path=str(tmp_path / 'files-matrix.png'), full_page=True)
        page.get_by_label('From', exact=True).select_option('compute')
        playwright.expect(page.locator('[data-file-empty]')).to_be_visible()
        page.get_by_label('From', exact=True).select_option('storage')
        page.get_by_label('Search files', exact=True).fill('alpha')
        page.get_by_label('Select all visible files', exact=True).check()
        playwright.expect(page.get_by_label('Select alpha.gguf', exact=True)).to_be_checked()
        playwright.expect(page.get_by_label('Select beta.gguf', exact=True)).not_to_be_checked()
        # Let a real scheduled refresh run: unsent selection, search, and source must survive.
        page.wait_for_timeout(5300)
        playwright.expect(page.get_by_label('Select alpha.gguf', exact=True)).to_be_checked()
        playwright.expect(page.get_by_label('Search files', exact=True)).to_have_value('alpha')
        playwright.expect(page.get_by_label('From', exact=True)).to_have_value('storage')
        playwright.expect(page.locator('[data-file-operation] option[value="move"]')).to_be_disabled()
        playwright.expect(page.get_by_label('To', exact=True)).to_have_value('compute')
        page.locator('[data-transfer-files]').click()
        page.locator('[data-transfer-files]').click()
        finished(browser)
        assert (node / 'llm' / 'alpha.gguf').exists() and not (node / 'llm' / 'beta.gguf').exists()
        page.goto('http://console.test/files')
        playwright.expect(page.locator('[data-file-row]')).to_have_count(2)
        # The refreshed form is initialized too, including direct matrix edits.
        page.get_by_label('beta.gguf in compute', exact=True).check()
        page.get_by_label('Select beta.gguf', exact=True).check()
        playwright.expect(page.locator('[data-transfer-files]')).to_be_disabled()
        playwright.expect(page.locator('[data-transfer-note]')).to_contain_text('Apply or undo')
        playwright.expect(page.locator('[data-matrix-changes]')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / 'files-matrix-narrow.png'), full_page=True)
        assert not errors
        chrome.close()


def test_a_node_box_copies_in_bulk_and_shows_progress(library_desk, monkeypatch):
    from test_console_browser import browser_context, playwright
    from turnstile.artifacts import sha256_of
    browser, compute, storage, node = library_desk
    source = Path(gguf(storage / 'llm' / 'browser.gguf', general__architecture='qwen'))
    release = threading.Event()

    def slow_hash(path, *args, **kwargs):
        if not release.wait(15):
            raise OSError('The checksum was not released by the browser test.')
        return sha256_of(path, *args, **kwargs)

    monkeypatch.setattr('turnstile.artifacts.sha256_of', slow_hash)
    browser.post('/files', data={'do': 'refresh-files'})
    with playwright.sync_playwright() as engine:
        chrome, page = browser_context(engine, browser)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto('http://console.test/files')
            page.get_by_role('checkbox', name=f'{source.name} in compute', exact=True).check()
            playwright.expect(page.locator('[data-file-boxes-summary]')).to_have_text('1 copy to make. The storage copy always stays.')
            page.get_by_role('button', name='Apply changes', exact=True).click()
            page.get_by_role('button', name='Are you sure? Apply changes', exact=True).click()  # two steps, as for the other file changes
            playwright.expect(page.locator('#files-distributions')).to_contain_text('Bulk change running')
            waiting = page.get_by_role('checkbox', name=f'{source.name} in compute: Waiting for the bulk change running now.', exact=True)
            playwright.expect(waiting).to_be_disabled()
            # No second change can start while this one runs: both ways to start one are disabled.
            playwright.expect(page.locator('[data-apply-boxes]')).to_be_disabled()
            playwright.expect(page.locator('[data-transfer-files]')).to_be_disabled()
            assert 'The form could not be submitted' not in page.content()
            assert source.is_file() and not (node / 'llm' / source.name).exists()
            release.set()
            playwright.expect(page.locator('#files-distributions')).to_contain_text('Bulk change finished: 1 of 1 done.', timeout=15000)
            assert source.read_bytes() == (node / 'llm' / source.name).read_bytes()
            browser.post('/files', data={'do': 'refresh-files'})  # the node looks through its folders again
            page.reload()
            playwright.expect(page.get_by_role('checkbox', name=f'{source.name} in compute', exact=True)).to_be_checked()
            assert not errors
        finally:
            release.set()
            chrome.close()
