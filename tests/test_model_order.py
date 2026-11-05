"""Checks of a model's sort order: the one order models are listed in everywhere."""

from pathlib import Path

import hashlib
import tomllib

import pytest
from typer.testing import CliRunner

from loadcoach.cli import app, model_command
from loadcoach.config import Config, load_config
from loadcoach.log import Log
from loadcoach.models import Models
from test_app import fake_llama_server, gateway, gguf, model  # noqa: F401
from test_postgresql import postgres  # noqa: F401
from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)


def test_models_are_listed_by_sort_order_and_then_by_name(tmp_path):
    kept = Models(str(tmp_path / "models.db"))
    for name in ("zeta", "Beta", "alpha", "gamma"):
        kept.put(name, {"flags": {"model": f"{name}.gguf", "ctx-size": 2048}})
    # Every model starts at 10: by name, whatever the case.
    assert kept.names() == ["alpha", "Beta", "gamma", "zeta"] and {kept.place(name) for name in kept.names()} == {10} and kept.place("nope") is None
    kept.set_place("zeta", 1)
    kept.set_place("gamma", 5)
    kept.set_place("Beta", 5)
    kept.set_place("alpha", 11)
    kept.put("delta", {"flags": {"model": "delta.gguf", "ctx-size": 2048}})  # one added later starts at 10 too: after the 5s and before the 11
    assert kept.names() == ["zeta", "Beta", "gamma", "delta", "alpha"] and kept.place("zeta") == 1
    before = kept.stored("zeta")
    kept.rename("zeta", "omega")  # the sort order follows a model to its new name, and is no part of its settings
    kept.set_place("gamma", None)  # back to 10
    assert kept.names() == ["omega", "Beta", "delta", "gamma", "alpha"] and kept.place("gamma") == 10 and kept.stored("omega") == before
    assert Models(str(tmp_path / "models.db")).names() == kept.names()  # kept in the file, and read back


def test_the_sort_order_is_set_on_a_models_page_and_every_list_follows_it(desk, tmp_path):
    browser, _, _ = desk
    kept = Models(str(tmp_path / "loadcoach.db"), {"flash-attn": "on"})
    for name in ("apple", "mango", "zebra"):
        kept.put(name, {"gpus": 1, "flags": {"model": gguf(tmp_path / f"{name}.gguf"), "ctx-size": 2048}})
    sign_in(browser, "jordan")
    form = {"file": str(tmp_path / "zebra.gguf"), "ctx": "2048", "cards": "1"}
    refused = browser.post("/models/zebra", data={**form, "order": "first"})
    assert refused.status_code == 400 and "order is a whole number" in refused.text and 'name="order" type="number" step="1" class="form-control" value="first"' in refused.text
    saved = browser.post("/models/zebra", data={**form, "order": "0"})
    assert "Saved." in saved.text and 'name="order" type="number" step="1" class="form-control" value="0"' in saved.text and kept.place("zebra") == 0
    assert kept.stored("zebra") == {"gpus": 1, "flags": {"model": str(tmp_path / "zebra.gguf"), "ctx-size": 2048}}  # not among its settings

    listed = browser.get("/models?part=list").text
    assert listed.index('href="/models/zebra"') < listed.index('href="/models/apple"') < listed.index('href="/models/mango"')
    assert '<td data-value="0">0</td>' in listed and listed.count('<td data-value="10">10</td>') == 2
    fit = browser.get("/dry-fit").text
    assert fit.index('href="/models/zebra"') < fit.index('href="/models/apple"')

    # A prior result must not move a model below models that have not run yet.
    store = browser.app.state.benchmarks.state.store
    run = store.create("jordan", "zebra", "extraction")
    store.activate(run, {"fingerprint": "saved", "subject": {}, "environment": {}})
    store.finish(run, "completed")
    start = browser.get("/benchmarks/runs?benchmark=extraction").text.split('id="benchmark-models"', 1)[1]
    assert start.index("zebra") < start.index("apple") < start.index("mango")

    emptied = browser.post("/models/zebra", data=form)  # the box left empty puts it back at 10, where every model starts
    assert 'name="order" type="number" step="1" class="form-control" value="10"' in emptied.text and kept.place("zebra") == 10 and kept.names() == ["apple", "mango", "zebra"]


def test_a_program_is_told_the_models_in_their_sort_order(fake_llama_server):  # noqa: F811
    fixtures = Path(__file__).resolve().parent / "fixtures" / "gguf"
    models = {name: model(str(fixtures / f"{name}.gguf")) for name in ("apple", "mango", "zebra")}
    with gateway(fake_llama_server, models) as caller:
        ids = lambda: [one["id"] for one in caller.get("/v1/models").json()["data"]]  # noqa: E731
        assert ids() == ["apple", "mango", "zebra"]
        assert caller.post("/loadcoach/models/zebra", json={**models["zebra"], "order": 2}).status_code == 200
        assert caller.post("/loadcoach/models/mango", json={**models["mango"], "order": 1}).status_code == 200
        assert caller.post("/loadcoach/models/mango", json=models["mango"]).status_code == 200  # saved without one: it keeps the one it has
        listed = caller.get("/loadcoach/models").json()["models"]
        offered = caller.get("/v1/models").json()["data"]
        assert ids() == ["mango", "zebra", "apple"] and [(one["id"], one["order"]) for one in offered] == [("mango", 1), ("zebra", 2), ("apple", 10)]
        assert [(one["name"], one["order"]) for one in listed] == [("mango", 1), ("zebra", 2), ("apple", 10)]
        assert caller.post("/loadcoach/models/zebra", json={**models["zebra"], "order": 1.5}).status_code == 400
        assert caller.post("/loadcoach/models/zebra", json={**models["zebra"], "flags": {**models["zebra"]["flags"], "ctx-size": 4096}, "order": 2**31}).status_code == 400
        assert next(one for one in caller.get("/loadcoach/models").json()["models"] if one["name"] == "zebra")["flags"]["ctx-size"] == models["zebra"]["flags"]["ctx-size"]
        assert caller.post("/loadcoach/models/zebra", json={**models["zebra"], "order": None}).status_code == 200
        assert ids() == ["mango", "apple", "zebra"]  # zebra is back at 10, beside apple
        assert caller.post("/loadcoach/models/zebra", json={**models["zebra"], "order": 0}).status_code == 200
        assert caller.get("/v1/models").json()["data"][0]["order"] == 0


def test_cli_order_preserves_identity_fingerprint_and_fit_evidence(tmp_path, capsys):
    kept = Models(str(tmp_path / "models.db"))
    file = tmp_path / "model.gguf"
    file.write_bytes(b"model data")
    kept.put("zeta", {"flags": {"model": str(file), "ctx-size": 2048}})
    kept.keep_hash(str(file), hashlib.sha256(file.read_bytes()).hexdigest(), file.stat().st_size, file.stat().st_mtime)
    kept.measured("zeta", kept.get("zeta"), [100])
    identity, fingerprint, evidence = kept.identity("zeta"), kept.fingerprint(kept.get("zeta"), "zeta"), kept.measurements("zeta")
    config, log = Config(), Log(str(tmp_path / "models.db"))
    model_command(kept, config, "order", ["zeta", "-5"], log)
    assert kept.place("zeta") == -5 and kept.identity("zeta") == identity
    assert kept.fingerprint(kept.get("zeta"), "zeta") == fingerprint and kept.measurements("zeta") == evidence
    capsys.readouterr()
    model_command(kept, config, "list", [])
    assert "ORDER" in capsys.readouterr().out
    model_command(kept, config, "show", ["zeta"])
    shown = tomllib.loads(capsys.readouterr().out)["models"]["zeta"]
    assert shown == {**kept.stored("zeta"), "order": -5} and "order" not in kept.stored("zeta")
    model_command(kept, config, "order", ["zeta", "default"], log)
    assert kept.place("zeta") == 10
    with pytest.raises(ValueError, match="separate"):
        model_command(kept, config, "set", ["zeta", "ctx-size=4096", "order=2"])
    assert kept.get("zeta")["flags"]["ctx-size"] == 2048


def test_cli_exports_order_and_new_config_takes_it_over_only_once(tmp_path, capsys):
    kept = Models(str(tmp_path / "original.db"))
    for name in ("alpha", "zeta"):
        kept.put(name, {"gpus": 0, "flags": {"model": f"/{name}.gguf", "ctx-size": 2048}})
    kept.set_place("zeta", -1)
    model_command(kept, Config(), "export", [])
    file = tmp_path / "loadcoach.toml"
    file.write_text(capsys.readouterr().out)
    config = load_config(file)
    imported = Models(config.database, config.model_defaults, config.models)
    assert imported.names() == kept.names() == ["zeta", "alpha"]
    assert [imported.place(name) for name in imported.names()] == [-1, 10]
    assert all(imported.stored(name) == kept.stored(name) for name in kept.names())
    imported.set_place("alpha", -10)
    assert Models(config.database, first=config.models).names() == ["alpha", "zeta"]


@pytest.mark.parametrize("value", ["1.5", "true", "first", str(2**31), str(-(2**31)-1)])
def test_invalid_cli_order_cannot_change_the_model(tmp_path, value):
    file = tmp_path / "loadcoach.toml"
    file.write_text('[models.one]\ngpus=0\n[models.one.flags]\nmodel="/one.gguf"\nctx-size=2048\n')
    run = lambda *words: CliRunner().invoke(app, ["--config", str(file), "models", *words])
    assert run("order", "one", "1").exit_code == 0
    refused = run("order", "--", "one", value)
    assert refused.exit_code != 0 and "whole number" in str(refused.exception)
    assert Models(str(tmp_path / "loadcoach.db")).place("one") == 1
    assert run("order", "--", "one", "-5").exit_code == 0
    assert Models(str(tmp_path / "loadcoach.db")).place("one") == -5
    assert run("order", "one", "default").exit_code == 0
    assert Models(str(tmp_path / "loadcoach.db")).place("one") == 10


@pytest.mark.parametrize("value", [True, 1.5, 2**31, -(2**31)-1])
def test_config_rejects_nonportable_order_before_taking_over_any_model(tmp_path, value):
    file = tmp_path / "loadcoach.toml"
    literal = str(value).lower()
    file.write_text(f'[models.one]\ngpus=0\norder={literal}\n[models.one.flags]\nmodel="/one.gguf"\nctx-size=2048\n')
    with pytest.raises(SystemExit, match="order is a whole number"):
        load_config(file)


def test_postgresql_model_order_accepts_exported_values_and_keeps_evidence(postgres):  # noqa: F811
    settings = {"flags": {"model": "/one.gguf", "ctx-size": 2048}}
    kept = Models(postgres, first={"one": {**settings, "order": -(2**31)}, "two": settings})
    identity = kept.identity("one")
    assert kept.names() == ["one", "two"] and kept.stored("one") == settings
    kept.set_place("one", 2**31-1)
    with pytest.raises(ValueError, match="order is a whole number"):
        kept.set_place("one", 2**31)
    assert kept.names() == ["two", "one"] and kept.identity("one") == identity
