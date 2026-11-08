"""The kept list of model files the Files page answers from, instead of reading every file on each visit."""

import json
import time
from pathlib import Path

import pytest

from loadcoach.file_index import FileIndex
from test_app import add_user, fake_llama_server, gateway, gguf, model  # noqa: F401
from test_spotter import made_up, wait_until
from test_weightroom import desk, sign_in  # noqa: F401


def counting():
    """A describe that says which files it was asked to read, so a test can see what was read again."""
    read = []

    def describe(path: Path) -> dict:
        read.append(path.name)
        return {"what": "model", "name": path.stem}
    return read, describe


def test_a_look_reads_only_the_files_that_are_new_or_have_changed(tmp_path):
    folder = tmp_path / "llm"
    folder.mkdir()
    (folder / "a.gguf").write_bytes(b"a")
    (folder / "b.gguf").write_bytes(b"b")
    read, describe = counting()
    index = FileIndex(tmp_path / "files-index.json", describe=describe)
    spots = {"llm": str(folder)}
    first = index.scan(spots)
    assert sorted(read) == ["a.gguf", "b.gguf"] and set(map(lambda path: Path(path).name, first["files"])) == {"a.gguf", "b.gguf"}

    read.clear()
    index.scan(spots)
    assert read == []  # nothing changed, nothing read

    (folder / "b.gguf").write_bytes(b"bigger now")
    (folder / "c.gguf").write_bytes(b"c")
    (folder / "a.gguf").unlink()
    kept = index.scan(spots)
    assert sorted(read) == ["b.gguf", "c.gguf"]
    assert {Path(path).name: entry["size"] for path, entry in kept["files"].items()} == {"b.gguf": 10, "c.gguf": 1}
    # Another program on the machine reads the same list from the file.
    assert set(json.loads((tmp_path / "files-index.json").read_text())["files"]) == set(kept["files"])


def test_the_kept_list_is_used_until_a_refresh_its_age_its_places_or_a_wide_change_says_otherwise(tmp_path):
    folder = tmp_path / "llm"
    folder.mkdir()
    (folder / "a.gguf").write_bytes(b"a")
    _, describe = counting()
    index, spots = FileIndex(None, describe=describe), {"llm": str(folder)}
    names = lambda kept: sorted(Path(path).name for path in kept["files"])  # noqa: E731
    assert names(index.current(spots)) == ["a.gguf"]
    (folder / "new.gguf").write_bytes(b"n")
    assert names(index.current(spots)) == ["a.gguf"]  # the kept list, until it is looked through again
    assert names(index.current(spots, refresh=True)) == ["a.gguf", "new.gguf"]
    (folder / "later.gguf").write_bytes(b"l")
    index.kept["checked"] = time.time() - 120
    assert names(index.current(spots, every=600)) == ["a.gguf", "new.gguf"]  # not old enough yet
    assert names(index.current(spots, every=60)) == ["a.gguf", "later.gguf", "new.gguf"]
    (folder / "after-cleanup.gguf").write_bytes(b"c")
    index.stale()
    assert "after-cleanup.gguf" in names(index.current(spots))
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.gguf").write_bytes(b"x")
    assert names(index.current({"other": str(other)})) == ["x.gguf"]  # other places: looked through at once


def test_a_change_made_by_the_page_puts_its_own_lines_right(tmp_path):
    folder = tmp_path / "llm"
    folder.mkdir()
    (folder / "a.gguf").write_bytes(b"a")
    read, describe = counting()
    index, spots = FileIndex(tmp_path / "files-index.json", describe=describe), {"llm": str(folder)}
    index.update([folder / "a.gguf"], spots)
    assert index.read() is None  # no list yet: the first look reads everything anyway
    index.scan(spots)
    read.clear()
    (folder / "b.gguf").write_bytes(b"b")
    (folder / "a.gguf").unlink()
    (folder / "notes.txt").write_text("not a model")
    index.update([folder / "a.gguf", folder / "b.gguf", folder / "notes.txt", tmp_path / "outside.gguf"], spots)
    assert read == ["b.gguf"]
    assert [Path(path).name for path in index.read()["files"]] == ["b.gguf"]


def test_loadcoach_answers_from_the_kept_list_and_its_own_changes_show_at_once(fake_llama_server, tmp_path):
    used = gguf(tmp_path / "used.gguf", general__name="Used", general__architecture="tiny", tiny__context_length=4096)
    spare = gguf(tmp_path / "spare.gguf", general__name="Spare")
    with gateway(fake_llama_server, {"mine": model(used, 1)}) as caller:
        admin = add_user(caller, "jordan", "admin")
        listed = lambda **asked: sorted(Path(file["path"]).name for file in caller.get(  # noqa: E731
            "/loadcoach/files", headers=admin, params=asked).json()["files"])
        first = caller.get("/loadcoach/files", headers=admin).json()
        assert sorted(Path(file["path"]).name for file in first["files"]) == ["spare.gguf", "used.gguf"]
        assert first["checked"].endswith("+00:00")
        assert {file["path"]: file["used_by"] for file in first["files"]}[used] == ["mine"]  # which models use it is not kept: it is asked each time
        gguf(tmp_path / "copied-by-hand.gguf", general__name="By hand")
        assert listed() == ["spare.gguf", "used.gguf"]  # put there outside LoadCoach: shown at the next look
        assert listed(refresh="1") == ["copied-by-hand.gguf", "spare.gguf", "used.gguf"]
        deleted = caller.post("/loadcoach/files/delete", headers=admin, json={"path": spare, "name": "spare.gguf"})
        assert deleted.status_code == 200
        assert listed() == ["copied-by-hand.gguf", "used.gguf"]  # deleted from the page: gone from the list straight away


def test_spotter_looks_through_the_places_every_so_often_and_puts_a_received_file_in(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / "first.gguf").write_bytes(b"first")
    kept = tmp_path / "files-index.json"
    names = lambda: sorted(Path(path).name for path in json.loads(kept.read_text())["files"]) if kept.exists() else []  # noqa: E731
    with made_up(tmp_path, monkeypatch, places={"models": str(models)}, file_index=str(kept), files_every=0.001):
        assert wait_until(lambda: names() == ["first.gguf"])
        (models / "second.gguf").write_bytes(b"second")
        assert wait_until(lambda: names() == ["first.gguf", "second.gguf"])

    content = b"a complete model file"
    digest = __import__("hashlib").sha256(content).hexdigest()
    (tmp_path / "again").mkdir()
    with made_up(tmp_path / "again", monkeypatch, places={"models": str(models)}, file_index=str(kept), files_every=0) as (caller, keys, _, _, _):
        prepared = caller.post("/spotter/files/prepare", headers=keys["jordan"], json={"place": "models", "relative": "repo/model.gguf", "size": len(content), "sha256": digest})
        assert caller.put(prepared.json()["upload"], content=content).status_code == 201
    assert names() == ["first.gguf", "model.gguf", "second.gguf"]  # received, so listed without waiting for a look


def test_the_files_page_refresh_button_looks_again_on_front_and_every_node(desk, tmp_path):
    browser, compute, _ = desk
    compute.off = True
    sign_in(browser, "jordan")
    cache, storage = tmp_path / "front-cache", tmp_path / "front-storage"
    browser.post("/settings/model-files", data={"file_machine": "front", "models_folder": str(cache), "models_storage_folder": str(storage)})
    (storage / "llm" / "first.gguf").write_bytes(b"first")
    page = browser.get("/files?tier=storage&complete=1").text
    assert "first.gguf" in page and 'value="refresh-files"' in page and 'name="back" value="/files?tier=storage"' in page
    assert "Files last checked: Front <time" in page
    (storage / "llm" / "dropped-in.gguf").write_bytes(b"dropped in")
    assert "dropped-in.gguf" not in browser.get("/files?tier=storage&complete=1").text  # the kept list, until refreshed
    refreshed = browser.post("/files", data={"do": "refresh-files", "back": "/files?tier=storage"}, follow_redirects=False)
    said = refreshed.headers["location"]
    assert refreshed.status_code in (302, 303) and said.startswith("/files?tier=storage&said=Looked%20through")
    assert "not%20looked%20at%3A%20compute" in said  # an offline node is named, not waited for
    assert "dropped-in.gguf" in browser.get("/files?tier=storage&complete=1").text
    # Only the Files page itself can be the way back.
    away = browser.post("/files", data={"do": "refresh-files", "back": "https://elsewhere.example/files?"}, follow_redirects=False)
    assert away.headers["location"].startswith("/files?&said=")


@pytest.mark.parametrize("minutes,ok", [(0, True), (2.5, True), (-1, False)])
def test_the_check_interval_is_a_node_setting(tmp_path, minutes, ok):
    from spotter.app import load_settings
    from loadcoach.settings import CHANGEABLE
    assert "files_every" in CHANGEABLE["spotter"] and "files_every" in CHANGEABLE["turnstile"]
    (tmp_path / "loadcoach.toml").write_text('data = "data"\n')
    file = tmp_path / "spotter.toml"
    file.write_text(f'files_every = {minutes}\n[services]\nloadcoach = "loadcoach.service"\n')
    if not ok:
        with pytest.raises(SystemExit):
            load_settings(file)
        return
    settings = load_settings(file)
    assert settings.files_every == minutes and settings.file_index == str(tmp_path / "data" / "files-index.json")
