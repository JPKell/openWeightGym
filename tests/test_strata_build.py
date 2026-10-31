"""Build identity changes are tested on tiny fixtures, never by modifying installed files."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engines.strata import build, prepare
from test_strata_settings import node, settings


@pytest.fixture
def installation(node, tmp_path, monkeypatch):
    source = Path(node.source)
    (source/"serve").mkdir()
    (source/"serve/server.py").write_text("fixture upstream")
    venv_file = Path(node.python).parent.parent/"lib/python3.14/site-packages/example.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("fixture dependency")
    stdlib = tmp_path/"python3.14"
    stdlib.mkdir()
    (stdlib/"fixture.py").write_text("fixture stdlib")
    suite = tmp_path/"suite"
    (suite/"engines/strata").mkdir(parents=True)
    (suite/"engines/strata/build.py").write_text("fixture openWeightGym code")
    library = tmp_path/"linked.so"
    library.write_bytes(b"fixture linked library")
    monkeypatch.setattr(build, "__file__", str(suite/"engines/strata/build.py"))
    monkeypatch.setattr(build, "verify_source", lambda path: None)
    monkeypatch.setattr(build, "linked_files", lambda paths: [library])
    def run(command, **kwargs):
        if command[0] == node.python:
            assert "-I" in command and "-S" in command
            return SimpleNamespace(stdout=json.dumps({"version": "fixture Python", "stdlib": str(stdlib)}))
        assert command[:3] == ["git", "-C", node.source]
        return SimpleNamespace(stdout=b"serve/server.py\0")
    monkeypatch.setattr(build.subprocess, "run", run)
    return node


def test_build_identity_covers_source_native_environment_glue_and_libraries(installation):
    manifest = build.manifest(installation)
    assert manifest["schema"] == 1 and manifest["engine"] == "strata"
    assert {"native", "native_kq", "python", "venv_config", "upstream/serve/server.py",
            "suite/engines/strata/build.py", "stdlib/fixture.py"} <= manifest["files"].keys()
    assert any(name.startswith("environment/") for name in manifest["files"])
    assert any(name.startswith("linked/") for name in manifest["files"])
    assert build.verify(installation, manifest) == manifest
    for entry in manifest["files"].values():
        path = Path(entry["path"])
        original = path.read_bytes()
        path.write_bytes(original+b"changed")
        try:
            with pytest.raises(ValueError, match="installation changed"):
                build.verify(installation, manifest)
        finally:
            path.write_bytes(original)


def test_added_dependency_changes_identity_and_bytecode_is_never_a_hidden_input(installation):
    manifest = build.manifest(installation)
    library = Path(installation.python).parent.parent/"lib"
    (library/"new.pth").write_text("new dependency hook")
    with pytest.raises(ValueError, match="installation changed"):
        build.verify(installation, manifest)
    (library/"new.pth").unlink()
    (library/"__pycache__").mkdir()
    (library/"__pycache__/old.pyc").write_bytes(b"ignored only with isolated fresh-cache launch")
    assert build.manifest(installation) == manifest


def test_private_python_launch_never_reuses_bytecode_cache(node, tmp_path):
    command = build.python_command(node.python, "/fixture/runtime.py", str(tmp_path), "--config", "/private/config")
    assert command[:4] == [node.python, "-I", "-B", "-X"]
    assert command[4] == "pycache_prefix="+str(tmp_path/"python-cache")
    assert (tmp_path/"python-cache").stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        build.python_command(node.python, "/fixture/runtime.py", str(tmp_path))


def test_external_pth_search_paths_are_not_silently_untracked(installation):
    path = Path(installation.python).parent.parent/"lib/outside.pth"
    path.write_text("/untracked/external/site-packages\n")
    with pytest.raises(ValueError, match="untracked external"):
        build.manifest(installation)


def test_ram_policy_changes_invalidate_build_qualification(installation):
    manifest = build.manifest(installation)
    with pytest.raises(ValueError, match="installation changed"):
        build.verify(replace(installation, host_ram_mib=installation.host_ram_mib-1), manifest)


def test_prepared_bounded_launch_requires_matching_build_and_hides_key(node, settings, tmp_path, monkeypatch):
    from engines.strata.artifacts import manifest
    monkeypatch.setattr(prepare, "verify_source", lambda path: None)
    monkeypatch.setattr(build, "verify", lambda node, expected: None if expected == {"fixture": True} else
                        (_ for _ in ()).throw(ValueError("build changed")))
    with pytest.raises(ValueError, match="reviewed build"):
        with prepare.prepared_launch(node, settings, manifest(settings), None, gpu=0, port=54321, name="fixture", directory=str(tmp_path)):
            pass
    with prepare.prepared_launch(node, settings, manifest(settings), {"fixture": True}, gpu=0, port=54321,
                                 name="fixture", directory=str(tmp_path)) as launch:
        assert launch.memory_mib == node.host_ram_mib and launch.process_group and not launch.capture_output
        assert launch.headers["Authorization"].startswith("Bearer ")
        assert launch.headers["Authorization"] not in repr(launch)
        path = Path(launch.command[-1])
        assert json.loads(path.read_text())["build_manifest"] == {"fixture": True}
        assert "-I" in launch.command and "-B" in launch.command
    assert not path.exists() and not path.parent.exists()


def test_runtime_refuses_changed_build_before_importing_upstream(node, settings, monkeypatch):
    from engines.strata import runtime
    def refuse(node, expected):
        raise ValueError("fixture build changed")
    monkeypatch.setattr(build, "verify", refuse)
    monkeypatch.setattr(runtime, "verify_source", lambda path: pytest.fail("Changed build must refuse before upstream imports"))
    configuration = {"source":node.source,"executable":node.executable,"weights":settings["flags"]["model"],
        "tokenizer":settings["strata"]["tokenizer"],"context":512,"gpu":0,"host_ram_mib":22528,
        "model_name":"fixture","port":54321,"api_key":"fixture-"+"x"*32,"build_manifest":{"fixture":True}}
    with pytest.raises(ValueError, match="build changed"):
        runtime.run(configuration)
