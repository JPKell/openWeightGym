"""The Deploy page: front's source checkout, each machine's deployed commit and services, and jobs."""

import subprocess
import tomllib

import pytest

from weightroom import deploy as deploying
from test_weightroom import desk, sign_in  # noqa: F401


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.fixture
def plan(tmp_path, monkeypatch):
    """A source checkout one commit behind its origin, and one local machine whose services report active."""
    origin, source, home = tmp_path / "origin.git", tmp_path / "source", tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(source)], check=True, capture_output=True)
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "first"], ["push", "-q", "origin", "main"]):
        git(source, *args)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "second"], ["push", "-q", "origin", "main"]):
        git(other, *args)
    git(source, "fetch", "-q", "origin")
    (home / "node").mkdir()
    (home / "node" / "DEPLOYED").write_text(subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True).stdout)
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    (stand_ins / "systemctl").write_text('#!/bin/sh\nshift 2\nfor unit in "$@"; do echo active; done\n')
    (stand_ins / "systemctl").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{stand_ins}:/usr/bin:/bin")
    configured = {"source": str(source), "nodes": {"front": {"ssh": "local", "folder": "node", "extras": "benchmarks", "services": ["turnstile", "weightroom"]}}}
    real = deploying.checked
    monkeypatch.setattr(deploying, "checked", lambda deploy: real(configured))
    started = []
    monkeypatch.setattr(deploying, "running", lambda: False)
    monkeypatch.setattr(deploying, "start", lambda plan, logs, name, action, by: started.append((name, action, by)) or "20261022-120000-deploy-front")
    return started


def test_checked_refuses_anything_that_would_reach_a_shell_unchecked():
    good = {"ssh": "10.0.0.1", "folder": "suite", "extras": "voice,postgresql", "services": ["loadcoach"]}
    assert deploying.checked({"nodes": {"compute": good}})["nodes"]["compute"]["services"] == ["loadcoach"]
    for bad in ({"ssh": "host; rm -rf ~"}, {"ssh": "-oProxyCommand=x"}, {"folder": "-rf"}, {"folder": "../etc"}, {"folder": "/abs"}, {"extras": "x]; y"}, {"services": ["a b"]}, {"services": "loadcoach"}):
        with pytest.raises(ValueError):
            deploying.checked({"nodes": {"compute": {**good, **bad}}})


def test_deploy_page_shows_source_machines_and_starts_jobs_for_a_superuser_only(plan, desk):
    browser, _, _ = desk
    sign_in(browser, "jordan")  # admin, not superuser
    assert browser.get("/deploy").status_code == 403
    assert browser.post("/deploy", data={"do": "deploy", "node": "front"}).status_code == 403
    sign_in(browser, "sam")
    shown = browser.get("/deploy").text
    assert "first" in shown and "1 newer commit" in shown and "second" in shown
    assert "current" in shown and "turnstile" in shown and "text-bg-success" in shown
    assert 'href="/deploy"' in shown  # in the navigation
    pulled = browser.post("/deploy", data={"do": "pull"})
    assert "Source updated from GitHub." in pulled.text and "Up to date with GitHub" in pulled.text
    assert "differs from source" in pulled.text  # the machine still has the older commit
    started = browser.post("/deploy", data={"do": "deploy", "node": "front"})
    assert "Deploy of front started." in started.text and plan == [("front", "deploy", "sam")]
    assert "Set up a new machine" in shown and 'value="toollocker"' in shown
    unfiled = browser.post("/deploy", data={"do": "add", "name": "rig", "ssh": "10.0.0.9", "folder": "suite", "services": ["loadcoach"]})
    assert "without a settings file" in unfiled.text and len(plan) == 1
    refused = browser.post("/deploy", data={"do": "deploy", "node": "nowhere"})
    assert "configured machine" in refused.text and len(plan) == 1


def test_jobs_are_listed_newest_first_and_only_real_job_names_are_read(tmp_path):
    (tmp_path / "20261022-110000-restart-front.log").write_text("restart front\nexit=1\n")
    (tmp_path / "20261022-120000-deploy-compute.log").write_text("deploy compute\nInstalling\n")
    assert deploying.jobs(tmp_path) == [{"job": "20261022-120000-deploy-compute", "done": False, "ok": False},
                                        {"job": "20261022-110000-restart-front", "done": True, "ok": False}]
    assert "Installing" in deploying.read_job(tmp_path, "20261022-120000-deploy-compute")
    with pytest.raises(ValueError):
        deploying.read_job(tmp_path, "../../etc/passwd")


def test_a_new_machine_is_appended_to_the_settings_file_checked_and_only_once(tmp_path):
    settings = tmp_path / "weightroom.toml"
    settings.write_text('# front\ncompute = "http://compute"\n[deploy]\nsource = "~/suite-source"\n')
    deploying.add_node(settings, "rig", "jpk@10.0.0.9", "suite", ["voice", "loadcoach", "nonsense"])
    added = tomllib.loads(settings.read_text())["deploy"]
    assert added["source"] == "~/suite-source" and settings.read_text().startswith("# front\n")
    assert added["nodes"]["rig"] == {"ssh": "jpk@10.0.0.9", "folder": "suite", "extras": "voice", "services": ["loadcoach", "voice"]}
    for name, ssh, folder, services in (("rig", "10.0.0.9", "suite", ["spotter"]), ("other", "-oProxyCommand=x", "suite", ["spotter"]),
                                        ("other", "10.0.0.9", "../x", ["spotter"]), ("other", "10.0.0.9", "suite", [])):
        with pytest.raises(ValueError):
            deploying.add_node(settings, name, ssh, folder, services)
    assert list(tomllib.loads(settings.read_text())["deploy"]["nodes"]) == ["rig"]


def test_setup_runs_the_setup_mode_and_refuses_services_install_suite_cannot_make(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(deploying.subprocess, "run", lambda argv, **_: ran.append(argv))
    plan = deploying.checked({"source": str(tmp_path), "nodes": {
        "rig": {"ssh": "10.0.0.9", "folder": "suite", "extras": "", "services": ["loadcoach", "spotter"]},
        "front": {"ssh": "local", "folder": "weightroom", "extras": "", "services": ["weightroom"]}}})
    job = deploying.start(plan, tmp_path / "logs", "rig", "setup", "sam")
    assert "setup-rig" in job and "scripts/deploy-node.sh --setup 10.0.0.9 suite loadcoach,spotter" in ran[0][-1]
    with pytest.raises(ValueError):
        deploying.start(plan, tmp_path / "logs", "front", "setup", "sam")
