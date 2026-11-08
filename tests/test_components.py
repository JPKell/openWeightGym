"""System → Components: which services and engines each machine has, installed or not, and nothing changed to find out."""

import asyncio
import os
import re
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import machine_key
from loadcoach import components, statement
from loadcoach.groups import Group
from loadcoach.users import Users
from test_app import add_user, fake_llama_server, gateway  # noqa: F401
from test_two_machines import Two
from test_weightroom import desk, sign_in  # noqa: F401
from turnstile.app import Settings as FrontSettings, build_app as build_front


def told(**units) -> dict:
    """What systemctl show says of each named unit: its load state and, when loaded, how it is doing."""
    return {name: {"Id": name, "LoadState": load, "ActiveState": active, "SubState": sub}
            for name, (load, active, sub) in units.items()}


@pytest.fixture
def systemd(monkeypatch):
    """systemd as each manager would answer, set per test as {"user": ..., "system": ...}; None is a manager that cannot be asked."""
    managers = {"user": {}, "system": {}}
    asked = []

    async def units(names, own):
        asked.append((tuple(names), own))
        return managers["user" if own else "system"]
    monkeypatch.setattr(components, "units", units)
    managers["asked"] = asked
    return managers


def by_id(rows):
    return {row["id"]: row for row in rows}


def has(row: dict, **values) -> bool:
    """Whether the row says each of these, whatever else it says."""
    return {field: row.get(field) for field in values} == values


def test_every_service_is_listed_with_where_its_unit_was_found(systemd):
    systemd["user"] = told(**{"loadcoach.service": ("loaded", "active", "running"), "voice.service": ("not-found", "inactive", "dead"),
                              "frontdesk.service": ("loaded", "inactive", "dead")})
    systemd["system"] = told(**{"spotter.service": ("loaded", "failed", "failed"), "turnstile.service": ("masked", "inactive", "dead")})
    rows = asyncio.run(components.services(running_here=("toollocker",)))
    assert [row["id"] for row in rows] == [name for name, _, _ in components.SERVICES]  # every service, in the installer's order
    found = by_id(rows)
    assert has(found["loadcoach"], installed=True, unit="loadcoach.service", scope="user", state="running")
    assert found["spotter"]["installed"] and found["spotter"]["scope"] == "system" and found["spotter"]["state"] == "failed"
    assert found["weightroom"]["unit"] == "frontdesk.service" and found["weightroom"]["state"] == "stopped"  # its name before the rename
    assert found["turnstile"]["installed"] and found["turnstile"]["state"] == "masked"
    assert found["toollocker"]["installed"] and found["toollocker"]["detail"] == "running without a systemd unit"
    assert not found["voice"]["installed"] and not found["voice-gateway"]["installed"]
    asked_for = systemd["asked"][0][0]
    assert "frontdesk.service" in asked_for and "tooldoor.service" in asked_for and len(systemd["asked"]) == 2


def test_a_machine_whose_systemd_cannot_be_asked_says_unknown_rather_than_missing(systemd):
    systemd["user"] = systemd["system"] = None
    found = by_id(asyncio.run(components.services(running_here=("turnstile",))))
    assert found["loadcoach"]["installed"] is None
    assert found["turnstile"]["installed"] is True  # it is answering this very question


def test_units_reads_what_systemctl_prints_and_only_ever_asks_it_to_show(tmp_path, monkeypatch):
    stand_ins, asked = tmp_path / "bin", tmp_path / "asked.txt"
    stand_ins.mkdir()
    (stand_ins / "systemctl").write_text(f"""#!/bin/sh
echo "$@" >> {asked}
[ "$1" = --user ] || exit 1
printf 'Id=loadcoach.service\\nLoadState=loaded\\nActiveState=active\\nSubState=running\\n\\nId=voice.service\\nLoadState=not-found\\nActiveState=inactive\\nSubState=dead\\n'
""")
    (stand_ins / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}:/usr/bin:/bin")
    own = asyncio.run(components.units(["loadcoach.service", "voice.service"], True))
    assert own["loadcoach.service"]["ActiveState"] == "active" and own["voice.service"]["LoadState"] == "not-found"
    assert asyncio.run(components.units(["loadcoach.service"], False)) is None  # the system manager refused
    lines = asked.read_text().splitlines()
    assert lines == ["--user show loadcoach.service voice.service -p Id,LoadState,ActiveState,SubState",
                     "--no-ask-password show loadcoach.service -p Id,LoadState,ActiveState,SubState"]
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert asyncio.run(components.units(["loadcoach.service"], True)) is None  # no systemctl at all


def test_engines_are_installed_when_their_program_is_found(tmp_path):
    program = tmp_path / "llama-server"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    found = by_id(components.engines(SimpleNamespace(engines={}, llama_server=str(program))))
    assert list(found) == ["llama_cpp", "strata", "sdcpp", "audiocpp"]
    assert has(found["llama_cpp"], installed=True, enabled=True, available=True)
    assert not found["strata"]["installed"] and not found["sdcpp"]["installed"] and found["sdcpp"]["why"] == ""
    off = by_id(components.engines(SimpleNamespace(engines={"llama_cpp": {"enabled": False}}, llama_server=str(program))))
    assert off["llama_cpp"]["installed"] and not off["llama_cpp"]["enabled"] and off["llama_cpp"]["why"] == "Disabled on this node."
    missing = by_id(components.engines(SimpleNamespace(engines={}, llama_server=str(tmp_path / "gone"))))
    assert missing["llama_cpp"]["installed"] is False


def test_front_reads_loadcoachs_settings_file_or_lists_every_engine_as_missing(tmp_path):
    none_here = components.engines_from_settings(tmp_path / "loadcoach.toml")
    assert [row["id"] for row in none_here] == ["llama_cpp", "strata", "sdcpp", "audiocpp"]
    assert not any(row["installed"] for row in none_here)
    program = tmp_path / "llama-server"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    (tmp_path / "loadcoach.toml").write_text(f'llama_server = "{program}"\n')
    assert by_id(components.engines_from_settings(tmp_path / "loadcoach.toml"))["llama_cpp"]["installed"] is True
    (tmp_path / "loadcoach.toml").write_text('[engines.strata]\nenabled = "yes"\n')
    unreadable = components.engines_from_settings(tmp_path / "loadcoach.toml")
    assert all(row["installed"] is None and "could not be read" in row["why"] for row in unreadable)


def test_the_receipt_shows_only_what_is_worth_showing(tmp_path):
    assert components.receipt(tmp_path) is None
    (tmp_path / ".install-receipt").write_text("# made by install.sh\nversion=2\nstate=complete\ninstalled=2026-10-31T09:00:00Z\n"
                                               "mode=user\ndir=/home/someone/openWeightGym\ntopology=compute\nservices=loadcoach,spotter\n"
                                               "database=postgresql://secret\n")
    assert components.receipt(tmp_path) == {"topology": "compute", "services": "loadcoach,spotter", "mode": "user",
                                            "installed": "2026-10-31T09:00:00Z", "state": "complete"}


def test_loadcoach_says_what_its_machine_has_to_whoever_manages_settings(fake_llama_server, systemd):
    systemd["user"] = told(**{"spotter.service": ("loaded", "active", "running")})
    with gateway(fake_llama_server) as caller:
        owner = add_user(caller, "owner", "admin")
        guest = add_user(caller, "guest", "plain", by=owner)
        assert caller.get("/loadcoach/components", headers=guest).status_code == 403
        found = caller.get("/loadcoach/components", headers=owner).json()
    services, engines = by_id(found["services"]), by_id(found["engines"])
    assert services["loadcoach"]["installed"] and services["spotter"]["state"] == "running" and not services["voice"]["installed"]
    assert engines["llama_cpp"]["installed"] and "build" in engines["llama_cpp"] and not engines["sdcpp"]["installed"]
    assert found["hostname"] == socket.gethostname()


def test_front_lists_itself_and_every_node_and_says_why_one_cannot_be_checked(tmp_path, systemd):
    systemd["user"] = told(**{"turnstile.service": ("loaded", "active", "running"), "weightroom.service": ("loaded", "active", "running")})
    users = Users(str(tmp_path / "accounts.db"), {"operator": Group(can=frozenset({"manage-settings"})), "guest": Group(can=frozenset({"load"})),
                                                  "system": Group(can=frozenset({"follow-settings"}))})
    for name, group in (("owner", "operator"), ("guest", "guest"), ("weightroom", "system")):
        users.add(name, group)
    owner = {"authorization": "Bearer " + users.new_key("owner")}
    guest = {"authorization": "Bearer " + users.new_key("guest")}
    (tmp_path / "weightroom.key").write_text(users.new_key("weightroom"))  # front's own account, which its monitor asks as
    primary_key, rig_key, old_key = machine_key(tmp_path), str(tmp_path / "rig.key"), str(tmp_path / "old.key")
    statement.replace_key(rig_key, "r" * 40)
    statement.replace_key(old_key, "o" * 40)

    def worker(name, key, knows=True):
        async def answer(request):
            if request.url.path == "/loadcoach/status":
                return JSONResponse({"memory": {}, "loaded": [], "running": [], "waiting": [], "holds": [], "busy": 0})
            if request.url.path == "/loadcoach/components" and knows:
                return JSONResponse({"hostname": f"{name}-host", "receipt": {"topology": "compute"},
                                     "services": [{"id": "loadcoach", "label": "Model runner", "installed": True}],
                                     "engines": [{"id": "llama_cpp", "label": "llama.cpp", "installed": True}]})
            return JSONResponse({"error": {"message": "Not found"}}, status_code=404)
        return statement.Statements(Starlette(routes=[Route("/{path:path}", answer)]), key)

    class Wire(Two):
        async def handle_async_request(self, request):
            if request.url.port == 9542:
                raise httpx.ConnectError("asleep")
            if request.url.port == 9642:
                return await httpx.ASGITransport(app=worker("old", old_key, knows=False)).handle_async_request(request)
            return await super().handle_async_request(request)

    settings = FrontSettings(compute="compute", data=str(tmp_path), machine_key_file=primary_key, machines_poll_seconds=3600,
                             auth_key_file=str(tmp_path / "weightroom.key"), machines={
        "rig": {"compute": "http://rig:9442", "machine_key_file": rig_key},
        "asleep": {"compute": "http://127.0.0.1:9542", "machine_key_file": str(tmp_path / "asleep.key")},
        "old": {"compute": "http://old:9642", "machine_key_file": old_key}})
    statement.replace_key(str(tmp_path / "asleep.key"), "s" * 40)
    with TestClient(build_front(settings, httpx.AsyncClient(transport=Wire(worker("compute", primary_key), worker("rig", rig_key))),
                                users=users)) as front:
        assert front.get("/turnstile/components", headers=guest).status_code == 403
        for _ in range(100):  # the monitor's first look runs as turnstile starts; a node it found silent is not asked again
            states = {one["name"]: one["state"] for one in front.get("/turnstile/machines", headers=owner).json()["machines"]}
            if states["compute"] == states["rig"] == states["old"] == "ready":
                break
            time.sleep(0.05)
        found = front.get("/turnstile/components", headers=owner).json()
    nodes = {node["name"]: node for node in found["nodes"]}
    assert list(nodes) == ["front", "compute", "rig", "asleep", "old"]
    assert nodes["front"]["answering"] and nodes["front"]["hostname"] == socket.gethostname()
    assert by_id(nodes["front"]["services"])["turnstile"]["state"] == "running"
    assert not any(row["installed"] for row in nodes["front"]["engines"])  # front has no LoadCoach settings here
    assert nodes["rig"]["answering"] and nodes["rig"]["hostname"] == "rig-host" and not nodes["rig"]["with_front"]
    assert not nodes["asleep"]["answering"] and "not answering" in nodes["asleep"]["why"] and nodes["asleep"]["with_front"]
    assert not nodes["old"]["answering"] and "older openWeightGym" in nodes["old"]["why"]
    assert [row["id"] for row in found["services"]] == [name for name, _, _ in components.SERVICES]
    assert [row["id"] for row in found["engines"]] == ["llama_cpp", "strata", "sdcpp", "audiocpp"]
    users.db.close()


def test_the_console_page_shows_every_machine_with_each_component_installed_or_not(desk, systemd, tmp_path, monkeypatch):
    browser, switch, users = desk
    here = components.here

    async def older_order(*args, **kwargs):
        found = await here(*args, **kwargs)
        found['services'].reverse()
        return found

    monkeypatch.setattr(components, 'here', older_order)
    # The desk's model runner names `llama-server`. Give it one, so the page does not depend on what this machine has installed.
    program = tmp_path / "bin" / "llama-server"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{program.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    systemd["user"] = told(**{"turnstile.service": ("loaded", "active", "running"), "loadcoach.service": ("loaded", "active", "running")})
    sign_in(browser, "pat")
    assert browser.get("/system/components").status_code == 403
    sign_in(browser, "jordan")
    shell = browser.get("/system/components")
    assert shell.status_code == 200 and 'id="components-report"' in shell.text and "data-deferred-loader" in shell.text
    assert 'href="/system/components" aria-current="page"' in shell.text  # listed under System, and the page shown
    part = browser.get("/system/components?part=page")
    assert part.status_code == 200 and part.text.lstrip().startswith('<div id="components-report">')
    cards = re.findall(r'<article class="card mb-3 component-card" aria-label="([^"]+)">(.*?)</article>', part.text, re.S)
    assert [title for title, _ in cards] == ["Front", "compute"]
    missing = {}
    for title, body in cards:
        rows = re.findall(r'<tr class="([^"]*)">\s*<th scope="row" class="fw-normal"><span class="component-name">([^<]+)</span>', body)
        # Every service and every engine is listed on every machine, installed or not.
        assert [name for _, name in rows] == [label for _, label, _ in components.SERVICES] + ["llama.cpp", "Strata", "stable-diffusion.cpp", "audio.cpp"]
        missing[title] = {name for kind, name in rows if "component-missing" in kind}
        assert body.count("Not installed") == len(missing[title]) and body.count("Installed<") == len(rows) - len(missing[title])
    # Front's console is the one showing this page; compute's model runner is the one that answered.
    assert {"Voice", "Monitor", "Tools", "Voice gateway"} <= missing["Front"] and not {"Web console", "API gateway", "Model runner"} & missing["Front"]
    assert "Model runner" not in missing["compute"] and "Web console" in missing["compute"] and "llama.cpp" not in missing["compute"]
    front, compute = (body for _, body in cards)
    assert "Same machine as front" in compute and "3 of 7 installed" in front and "2 of 7 installed" in compute
    whole = browser.get("/system/components?complete=1")
    assert whole.status_code == 200 and "data-deferred-loader" not in whole.text and "component-card" in whole.text
