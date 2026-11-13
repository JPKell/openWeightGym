"""Physical-host identity and front-owned telemetry across LoadCoach instances."""

import asyncio
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from loadcoach.log import Log
from turnstile.app import with_power_state
from turnstile.machines import Machines
from weightroom.app import worded


def test_two_instances_on_one_host_share_readings_and_front_history_survives_outage():
    log = Log(":memory:")
    clock = [1000.0]
    online = [True]
    spotter_online = [True]
    fingerprint = "a" * 64

    async def ask(name, path):
        if not online[0]:
            return None
        if path == "/v1/models":
            return {"data": []}
        return {"machine_fingerprint": fingerprint, "hostname": "front", "memory": {},
                "loaded": [{"model": "embed-small", "state": "ready", "gpus": []}] if name == "front_cpu" else [],
                "running": [], "waiting": []}

    async def telemetry(name, path):
        if not spotter_online[0]:
            return None
        return {"machine_fingerprint": fingerprint, "at": clock[0], "cards": [], 'up_for': 90061,
                'services': [{'name': 'loadcoach', 'unit': 'loadcoach.service', 'state': 'active',
                              'detail': 'running', 'installed': True, 'private_extra': 'omit'}],
                'may_act': True, 'power': {'may': True},
                "processor": {"load": 42, "temperature": None},
                "memory": {"used": 2_000, "total": 10_000}, "disks": []}

    machines = Machines(["compute", "front_cpu"], ask, now=lambda: clock[0],
                        ask_telemetry=telemetry, store=log.db)
    asyncio.run(machines.poll())
    hosts = machines.hosts()
    assert len(hosts) == 1 and hosts[0]["machines"] == ["compute", "front_cpu"]
    assert hosts[0]["telemetry"]["memory"]["used"] == 2_000
    assert hosts[0]['telemetry']['up_for'] == 90061
    assert hosts[0]['telemetry']['services'] == [{'name': 'loadcoach', 'unit': 'loadcoach.service',
                                               'state': 'active', 'detail': 'running', 'installed': True}]
    assert 'may_act' not in hosts[0]['telemetry'] and 'power' not in hosts[0]['telemetry']
    assert hosts[0]["loaded"] == [{"model": "embed-small", "state": "ready", "cards": [], "machine": "front_cpu"}]
    assert hosts[0]["models_unknown"] is False
    assert machines.history(fingerprint, 1000, 60, 2)["series"]["memory used"][-1] == 2_000

    online[0] = False
    clock[0] += 10
    asyncio.run(machines.poll())
    assert machines.report()[0]["state"] == "ready"  # routing's missed-poll grace is unchanged
    assert machines.hosts()[0]["loaded"] == [] and machines.hosts()[0]["models_unknown"] is True
    clock[0] += 30
    asyncio.run(machines.poll())
    assert machines.report()[0]["state"] == "not answering"  # placement still sees LoadCoach down
    assert machines.hosts()[0]["state"] == "ready"  # Spotter still sees the physical host
    assert machines.hosts()[0]["loaded"] == [] and machines.hosts()[0]["models_unknown"] is True
    spotter_online[0] = False
    clock[0] += 40
    asyncio.run(machines.poll())
    assert machines.hosts()[0]["state"] == "not answering"
    assert machines.hosts()[0]["loaded"] == [] and machines.hosts()[0]["models_unknown"] is True
    assert machines.history(fingerprint, 1080, 120, 2)["series"]["memory used"][-1] == 2_000
    restarted = Machines(["compute", "front_cpu"], ask, now=lambda: clock[0], store=log.db)
    assert restarted.hosts()[0]["fingerprint"] == fingerprint
    assert restarted.hosts()[0]["loaded"] == [] and restarted.hosts()[0]["models_unknown"] is True
    assert restarted.history(fingerprint, 1080, 120, 2)["series"]["memory used"][-1] == 2_000


def test_spotter_fingerprint_mismatch_never_attaches_readings_to_another_host():
    async def ask(name, path):
        return {"data": []} if path == "/v1/models" else {"machine_fingerprint": "a" * 64,
               "hostname": "compute", "memory": {}, "loaded": [], "running": [], "waiting": []}

    async def telemetry(name, path):
        return {"machine_fingerprint": "b" * 64, "memory": {"used": 1, "total": 2}}

    machines = Machines(["compute"], ask, ask_telemetry=telemetry)
    asyncio.run(machines.poll())
    assert machines.hosts()[0]["telemetry"] is None


def test_new_log_entries_include_physical_host_fingerprint():
    log = Log(":memory:")
    log.add("service", "started")
    detail = json.loads(log.db.execute("SELECT detail FROM events WHERE kind = 'service'").fetchone()[0])
    assert len(detail["machine_fingerprint"]) == 64


def test_telemetry_cards_show_individual_devices_and_compact_nav_stays_unchanged():
    from weightroom.telemetry_workspace import hosts_view
    pages = Path(__file__).parents[1] / "weightroom/pages"
    env = Environment(loader=FileSystemLoader(pages), autoescape=True)
    env.filters["worded"] = lambda value, name: "—" if value is None else worded(name, value)
    host = {"fingerprint": "a" * 64, "hostname": "compute", "machines": ["compute"],
            "state": "ready", "age": 1, "loaded": [{"model": "qwen-test", "state": "ready", "machine": "compute"}],
            "models_unknown": False, "telemetry": {"cards": [
                {"card": 0, "memory_used": 2**30, "memory_total": 2**31, "load": 100, "temperature": 99}],
                "memory": {"used": 2**30, "total": 2**32},
                "processor": {"load": 100, "temperature": 99}, "disks": []}}
    card = env.get_template("telemetry_parts.html").module.host_tile(hosts_view([host], 10)[0])
    assert 'GPU 0' in card and 'VRAM used' in card and '99°' in card and 'CPU 100%' in card
    assert "qwen-test" in env.get_template("host_models.html").render(host=host)
    host["loaded"] = []
    assert "Empty" in env.get_template("host_models.html").render(host=host)
    host["models_unknown"] = True
    assert "Unavailable" in env.get_template("host_models.html").render(host=host)
    strip = env.get_template("strip.html").render(hosts=[host])
    assert "compute" in strip and "VRAM" in strip and "GPU" in strip and "host-state-ready" in strip
    assert "background-color: hsl(30 60% 42%)" in strip
    states = []
    for state in ("sleeping", "not answering"):
        host["state"] = state
        states.append(env.get_template("strip.html").render(hosts=[host]))
    assert "host-state-sleeping" in states[0] and "host-state-not-answering" in states[1]


def test_telemetry_stream_defers_updates_while_a_select_has_focus():
    script = (Path(__file__).parents[1] / "weightroom/static/telemetry.js").read_text()
    assert 'new EventSource(strip.dataset.telemetryStream)' in script
    assert 'document.addEventListener("pointerdown"' in script
    assert 'if (isSelect(event.target)) selectOpen = true' in script
    assert 'pending = html' in script


def test_signed_sleep_notice_marks_only_its_own_host_sleeping_until_it_answers_again():
    hosts = [{"hostname": "main", "machines": ["jpk-main"], "state": "not answering"},
             {"hostname": "remote", "machines": ["remote"], "state": "not answering"}]
    notice = {"action": "sleep", "at": 20.0, "machine": "jpk-main"}
    shown = with_power_state(hosts, {"jpk-main": {"at": 10.0}}, notice)
    assert [host["state"] for host in shown] == ["sleeping", "not answering"]
    assert with_power_state(hosts, {"jpk-main": {"at": 21.0}}, notice) == hosts
    assert notice == {"action": None, "at": 0.0, "machine": "jpk-main"}
    # The notice of a node the front does not know marks nothing.
    assert with_power_state(hosts, {}, {"action": "sleep", "at": 20.0, "machine": ""}) == hosts


def test_host_wake_controls_are_node_specific_and_explain_missing_configuration():
    from types import SimpleNamespace
    pages = Path(__file__).parents[1] / "weightroom/pages"
    env = Environment(loader=FileSystemLoader(pages), autoescape=True)
    host = {"hostname": "gpu", "state": "not answering", "models_unknown": True,
            "machines": ["compute", "rig"], "wake_nodes": ["rig"]}
    template = env.get_template("host_wake.html")
    rendered = template.render(host=host, person=SimpleNamespace(can={"front-wake"}))
    assert '/telemetry/nodes/rig/wake' in rendered and 'Wake rig</button>' in rendered
    assert 'disabled title="Configure Wake-on-LAN for this node"' in rendered
    assert '<p' not in rendered
    assert '<form' not in template.render(host=host, person=SimpleNamespace(can=set()))
    assert '{% include "host_wake.html" %}' in (pages / 'telemetry_workspace.html').read_text()
    assert "extends 'machines.html'" in (pages / 'telemetry_host.html').read_text()


def test_node_capacity_keeps_an_explicit_empty_model_device_policy():
    async def ask(name, path):
        return {'data': []} if path == '/v1/models' else {
            'cards':['Intel@0000:00:02.0'], 'model_cards':[],
            'memory':{'Intel@0000:00:02.0':None}, 'loaded':[]}
    machines = Machines(['compute'], ask)
    asyncio.run(machines.poll())
    assert machines.report()[0]['cards'] == []
