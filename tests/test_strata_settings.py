"""CPU-only B1 preparation contracts. None of these register or launch Strata."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import engines
from engines.strata import artifacts, config, model, prepare, runtime
from loadcoach.models import Models, check_model


@pytest.fixture
def settings(tmp_path):
    weights = tmp_path / "model.gguf"
    weights.write_bytes(b"fixture-only-not-native-weights")
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    for filename in model.TOKENIZER_FILES:
        (tokenizer / filename).write_text("fixture-" + filename)
    return {"engine": "strata", "flags": {"model": str(weights), "ctx-size": 512},
            "strata": {"tokenizer": str(tokenizer)}, "gpus": "any"}


@pytest.fixture
def node(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    python = venv / "bin/python"
    python.write_text("fixture never executed")
    python.chmod(0o700)
    native = tmp_path / "native"
    native.mkdir()
    executable = native / "strata"
    executable.write_text("fixture never executed")
    executable.chmod(0o700)
    (native / "libstrata_kq.so").write_bytes(b"fixture-only")
    return config.NodeSettings.read({"enabled": True, "allow_host_ram": True, "host_ram_mib": 22528,
                                     "source": str(source), "python": str(python), "executable": str(executable)})


def test_node_configuration_is_disabled_by_default_and_never_executes(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Settings must not execute programs"))
    assert config.NodeSettings.read({}) == config.NodeSettings()
    with pytest.raises(ValueError, match="disabled"):
        config.NodeSettings().check_paths()
    for options in ({"enabled": "true"}, {"enabled": True}, {"enabled": True, "allow_host_ram": True},
                    {"allow_host_ram": 1}, {"host_ram_mib": True}, {"host_ram_mib": -1},
                    {"host_ram_mib": 2**31 + 1}, {"source": None}, {"source": "relative"},
                    {"python": "python"}, {"executable": "/bin/strata\0"}, {"hooks": "unsafe"}):
        with pytest.raises(ValueError):
            config.NodeSettings.read(options)


def test_installation_paths_require_private_environment_and_matching_library(node):
    node.check_paths()
    library = Path(node.executable).with_name("libstrata_kq.so")
    library.unlink()
    with pytest.raises(ValueError, match="libstrata_kq"):
        node.check_paths()
    library.write_bytes(b"fixture")
    Path(node.python).parent.parent.joinpath("pyvenv.cfg").unlink()
    with pytest.raises(ValueError, match="private Python"):
        node.check_paths()


def test_strata_model_is_typed_single_gpu_and_does_not_inherit_llama_defaults(settings):
    model.check_model("example", settings, ("CUDA0", "CUDA1"))
    assert model.effective(settings, {"gpu-layers": "all", "jinja": True, "parallel": 3}) == settings
    assert model.parallel_capacity(settings) == 1
    assert model.layouts(settings, ("CUDA0", "CUDA1")) == [{**settings, "gpus": 1}]
    assert model.required_devices(settings, ("CUDA0", "CUDA1")) == 1
    assert model.contexts({**settings, "min_context": 256}) == [256, 384, 512]
    assert model.at_context(settings, 256)["flags"]["ctx-size"] == 256
    assert settings["flags"]["ctx-size"] == 512
    assert model.base(settings)["engine"] == "strata"
    assert not model.embedding(settings) and not model.fixed_split(settings, 2)
    for value in (0, 2, True, [], ["CUDA0", "CUDA1"], ["missing"], [1], {}):
        with pytest.raises(ValueError):
            model.check_model("example", {**settings, "gpus": value}, ("CUDA0", "CUDA1"))
    for changed in ({"allow_split": False}, {"adapters": {}}, {"alone": "true"}, {"min_context": True},
                    {"min_context": 128}, {"min_context": 513}, {"engine": "llama_cpp"},
                    {"strata": {"tokenizer": "relative"}}, {"strata": {"tokenizer": "/x", "mtp": True}}):
        with pytest.raises(ValueError):
            model.check_model("example", {**settings, **changed}, ("CUDA0",))
    for flags in ({**settings["flags"], "parallel": 1}, {**settings["flags"], "jinja": True},
                  {**settings["flags"], "ctx-size": True}, {**settings["flags"], "ctx-size": 8193},
                  {**settings["flags"], "model": "relative"}):
        with pytest.raises(ValueError):
            model.check_model("example", {**settings, "flags": flags}, ("CUDA0",))


def test_model_form_has_no_raw_flag_escape_hatch(settings):
    submitted = {"file": [settings["flags"]["model"]], "ctx": ["512"],
                 "tokenizer": [settings["strata"]["tokenizer"]], "gpu_card": ["CUDA1"]}
    parsed = config.settings_from(submitted, lambda value: int(value) if value.isdigit() else value)
    model.check_model("example", parsed, ("CUDA0", "CUDA1"))
    assert parsed["gpus"] == ["CUDA1"] and parsed["engine"] == "strata"
    for key in ("flag", "value", "adapter", "projector", "cpu", "allow_split", "parallel_enabled"):
        with pytest.raises(ValueError):
            config.settings_from({**submitted, key: ["unsafe"]}, int)


def test_artifact_manifest_pins_every_input_and_fails_closed(settings, tmp_path):
    manifest = artifacts.manifest(settings)
    assert manifest["engine"] == "strata" and manifest["schema"] == 1
    assert set(manifest["files"]) == {"model", *model.TOKENIZER_FILES}
    assert artifacts.verify(settings, manifest) == manifest
    for field, path in model.files_of(settings).items():
        file = Path(path)
        before = file.read_bytes()
        file.write_bytes(before + b"changed")
        with pytest.raises(ValueError, match="changed"):
            artifacts.verify(settings, manifest)
        file.write_bytes(before)
    extra = copy.deepcopy(manifest)
    extra["files"]["unreviewed"] = extra["files"]["model"]
    with pytest.raises(ValueError):
        artifacts.verify(settings, extra)
    file = tmp_path / "fifo"
    os.mkfifo(file)
    with pytest.raises(ValueError, match="regular"):
        artifacts.identity(str(file))
    Path(model.files_of(settings)["merges.txt"]).unlink()
    with pytest.raises(FileNotFoundError):
        artifacts.verify(settings, manifest)


def test_artifact_replacement_during_hashing_is_rejected(settings, monkeypatch):
    target = Path(settings["flags"]["model"])
    original = hashlib.file_digest

    def replace(stream, algorithm):
        result = original(stream, algorithm)
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
        return result

    monkeypatch.setattr(hashlib, "file_digest", replace)
    with pytest.raises(ValueError, match="changed while hashing"):
        artifacts.identity(str(target))


def test_private_handoff_pins_artifacts_and_removes_key_on_error(settings, node, tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "verify_source", lambda path: None)
    manifest = artifacts.manifest(settings)
    with pytest.raises(RuntimeError, match="fixture cancellation"):
        with prepare.launch_config(node, settings, manifest, gpu=1, port=54321, name="fixture", directory=str(tmp_path)) as (path, key):
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
            data = json.loads(path.read_text())
            assert len(key) >= 32 and data["api_key"] == key
            assert data["artifact_manifest"] == manifest and data["gpu"] == 1
            assert data["host_ram_mib"] == node.host_ram_mib
            folder = path.parent
            raise RuntimeError("fixture cancellation")
    assert not folder.exists()
    Path(settings["flags"]["model"]).write_bytes(b"mutated before handoff")
    with pytest.raises(ValueError, match="changed"):
        with prepare.launch_config(node, settings, manifest, gpu=0, port=54321, name="fixture", directory=str(tmp_path)):
            pytest.fail("Changed inputs were accepted")
    assert not list(tmp_path.glob("strata-launch-*"))


def test_runtime_rechecks_handoff_before_importing_upstream(settings, node, tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "verify_source", lambda path: None)
    monkeypatch.setattr(runtime, "verify_source", lambda path: pytest.fail("Must refuse changed artifacts before importing upstream"))
    manifest = artifacts.manifest(settings)
    with prepare.launch_config(node, settings, manifest, gpu=0, port=54321, name="fixture", directory=str(tmp_path)) as (path, key):
        data = json.loads(path.read_text())
        Path(model.files_of(settings)["chat_template.jinja"]).write_text("changed after prepare")
        with pytest.raises(ValueError, match="changed"):
            runtime.run(data)


def test_shared_store_dispatches_defaults_artifacts_and_fingerprints_without_registering_strata(settings, monkeypatch):
    # Supply the not-yet-admitted engine only inside this isolated test registry.
    monkeypatch.setitem(engines._ENGINES, "strata", SimpleNamespace(id="strata", model=model))
    library = Models(":memory:", {"gpu-layers": "all", "parallel": 2, "jinja": True})
    check_model("strata-fixture", settings, ("CUDA0",))
    library.put("strata-fixture", settings)
    effective = library.get("strata-fixture")
    assert effective == settings and "parallel" not in effective["flags"]
    assert set(library.unhashed()) == set(model.files_of(settings).values())

    def hash_inputs():
        for path in model.files_of(settings).values():
            stat = os.stat(path)
            library.keep_hash(path, hashlib.sha256(Path(path).read_bytes()).hexdigest(), stat.st_size, stat.st_mtime)

    assert library.fingerprint(effective, "strata-fixture") is None
    hash_inputs()
    first = library.fingerprint(effective, "strata-fixture")
    assert first and library.fingerprints(first)[0]["settings"]["engine"] == "strata"
    assert set(library.fingerprints(first)[0]["files"]) == {"model", *model.TOKENIZER_FILES}
    library.measured("strata-fixture", effective, [1200])
    library.resident_measure("strata-fixture", effective, 2000)
    assert library.measure("strata-fixture")["mib"] == [1200]
    assert library.resident_measure("strata-fixture", effective) == 2000
    before = first
    for field, path in model.files_of(settings).items():
        Path(path).write_bytes(Path(path).read_bytes() + b"changed")
        assert library.fingerprint(effective, "strata-fixture") is None
        hash_inputs()
        changed = library.fingerprint(effective, "strata-fixture")
        assert changed and changed != before
        before = changed
        assert library.measure("strata-fixture") is None
        assert library.resident_measure("strata-fixture", effective) is None
    moved = copy.deepcopy(settings)
    moved["strata"]["tokenizer"] = str(Path(settings["strata"]["tokenizer"]).parent / "new-tokenizer")
    library.put("strata-fixture", moved)
    assert library.file_revision("strata-fixture") == 1


def test_presets_never_cross_engines_and_keep_target_tokenizer(settings, monkeypatch):
    from loadcoach import presets
    monkeypatch.setitem(engines._ENGINES, "strata", SimpleNamespace(id="strata", model=model))
    library = Models(":memory:")
    library.put("strata-fixture", settings)
    llama = {"flags": dict(settings["flags"]), "gpus": 1}
    library.put("llama-fixture", llama)
    library.save_preset("llama-fixture", "llama-tuning", {"settings": llama, "order": 10})
    assert library.presets("strata-fixture") == []
    library.save_preset("strata-fixture", "strata-tuning", {"settings": settings, "order": 10})
    assert [item["name"] for item in library.presets("llama-fixture")] == ["llama-tuning"]
    assert [item["name"] for item in library.presets("strata-fixture")] == ["strata-tuning"]
    assert library.preset_group("strata-fixture")["key"].startswith("engine:strata:")
    assert not library.preset_group("llama-fixture")["key"].startswith("engine:")
    other = copy.deepcopy(settings)
    other["strata"]["tokenizer"] = "/other/tokenizer"
    other["flags"]["model"] = "/other/model.gguf"
    other["flags"]["ctx-size"] = 256
    result = presets.for_target({"settings": other, "order": 12}, settings)
    assert result["settings"]["strata"] == settings["strata"]
    assert result["settings"]["flags"] == {"model": settings["flags"]["model"], "ctx-size": 256}
    assert other["strata"]["tokenizer"] == "/other/tokenizer"
    with pytest.raises(ValueError, match="across inference engines"):
        presets.for_target({"settings": llama, "order": 10}, settings)


def test_llama_fingerprint_serialization_remains_unchanged(tmp_path):
    weights, projector, adapter = (tmp_path / name for name in ("model.gguf", "projector.gguf", "adapter.gguf"))
    for file in (weights, projector, adapter):
        file.write_bytes(file.name.encode())
    library = Models(":memory:", {"jinja": True, "parallel": 1})
    settings = {"engine": "llama_cpp", "flags": {"model": str(weights), "mmproj": str(projector), "ctx-size": 512},
                "gpus": 1, "adapters": {"example": {"file": str(adapter), "scale": .5}}}
    library.put("example", settings)
    hashes = {}
    for file in (weights, projector, adapter):
        hashed = hashlib.sha256(file.read_bytes()).hexdigest()
        hashes[file] = hashed
        stat = file.stat()
        library.keep_hash(str(file), hashed, stat.st_size, stat.st_mtime)
    # This is the pre-extraction serialized shape, including inherited flags but not engine.
    historical = {"flags": {"jinja": True, "parallel": 1, "model": hashes[weights],
                            "mmproj": hashes[projector], "ctx-size": 512},
                  "gpus": 1, "adapters": [[hashes[adapter], .5]]}
    expected = hashlib.sha256(json.dumps(historical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert library.fingerprint(library.get("example"), "example") == expected
    library.rename("example", "renamed")
    assert library.fingerprint(library.get("renamed"), "renamed") == expected


def test_schema_matches_typed_settings_and_is_opt_in():
    schema = json.loads(Path(model.__file__).with_name("schema.json").read_text())
    assert {item["name"] for item in schema["node"]} == {field.name for field in config.fields(config.NodeSettings)}
    assert schema["selectable"] is True
    assert schema["capabilities"]["parallel"] == 1 and not schema["capabilities"]["native_probes"]
    assert engines.engine_for("strata").installation(SimpleNamespace(engines={})).enabled is False
