"""Checks of the dry fit page: what it is handed of each model, and what the roles would pick with the ticked models loaded."""

import json
import re

from weightroom.app import fit_cpu_limit, fit_cpu_mib, fit_line, fit_node
from loadcoach.models import Models, at_context
from loadcoach.roles import Roles
from test_app import gguf
from test_weightroom import desk, sign_in  # noqa: F401  (the first sets a check up)


def test_a_models_memory_is_drawn_through_what_it_measured_and_a_single_measure_gets_a_second_point_from_its_file():
    cards = ["A", "B"]
    measured = lambda *points: [{"context": context, "mib": mib} for context, mib in points]  # noqa: E731
    # A context whose load failed has no MiB, and is no point.
    ranged = fit_line({"gpus": 1, "size": 9 * 2**30, "measurements": measured((2000, [3000]), (6000, None), (10000, [11000]))}, cards)
    assert ranged == {"cards": cards, "need": 1, "points": [[2000, [3000]], [10000, [11000]]], "guessed": False}
    # Measured once, on both cards: with no context it takes what its file holds, divided as the measure was.
    once = fit_line({"size": 2000 * 2**20, "measurements": measured((8192, [3000, 1000]))}, cards)
    assert once["need"] == 2 and once["guessed"] and once["points"] == [[0, [1500, 500]], [8192, [3000, 1000]]]
    # A file larger than what was measured is partly on the processor: a smaller context is taken to free nothing.
    spilled = fit_line({"gpus": ["B"], "size": 9000 * 2**20, "measurements": measured((8192, [3000]))}, cards)
    assert spilled["cards"] == ["B"] and spilled["points"] == [[0, [3000]], [8192, [3000]]]
    assert fit_line({"gpus": 0, "size": 5, "measurements": []}, cards)["points"] == []


def test_a_dry_fit_uses_one_nodes_local_models_and_cpu_ram_measurement():
    known = {"nodes": {"compute": {"cards": ["CUDA0"]}, "cpu": {"cards": []}}, "models": [
        {"name": "same", "locations": [
            {"machine": "compute", "name": "same-gpu", "model_id": "g", "gpus": 1},
            {"machine": "cpu", "name": "same-cpu", "model_id": "c", "gpus": 0, "size": 100 * 2**20,
             "measurements": [{"context": 2048, "mib": None, "rss_mib": 700}]},
        ]}]}
    selected = fit_node(known, "cpu")
    assert selected["cards"] == [] and [(one["name"], one["local_name"]) for one in selected["models"]] == [("same", "same-cpu")]
    assert fit_cpu_mib(selected["models"][0]) == (700, False)
    assert fit_cpu_mib({"size": 100 * 2**20, "measurements": []}) == (356, True)
    assert fit_cpu_limit({'total':8192,'headroom':512,'service_limit':1500,
                          'service_available':200}) == 7680
    assert fit_cpu_limit({'total':8192,'headroom':512,'configured_limit':2048,
                          'service_limit':1500}) == 2048


def test_the_dry_fit_page_lists_measured_models_to_tick_and_says_what_each_role_would_pick_with_them_loaded(desk, tmp_path):
    browser, _, _ = desk
    kept, roles = Models(str(tmp_path / "loadcoach.db"), {"flash-attn": "on"}), Roles(str(tmp_path / "data" / "roles.db"))  # kept on the control node, beside the accounts
    kept.put("strong", {"gpus": 1, "min_context": 4096, "flags": {"model": gguf(tmp_path / "strong.gguf"), "ctx-size": 8192}})
    kept.put("quick", {"gpus": 1, "flags": {"model": gguf(tmp_path / "quick.gguf"), "ctx-size": 2048}})
    kept.put("new", {"gpus": 1, "flags": {"model": gguf(tmp_path / "new.gguf"), "ctx-size": 2048}})
    for context, mib in ((4096, 5000), (8192, 9000)):
        kept.measured("strong", at_context(kept.get("strong"), context), [mib])
    kept.measured("quick", kept.get("quick"), [1000])
    roles.put("chat", {"models": {"quick": 1, "strong": 2}})

    sign_in(browser, "pat")
    assert browser.get("/dry-fit").status_code == 403
    browser.post("/sign-out")
    sign_in(browser, "jordan")
    shown = browser.get("/dry-fit").text
    assert '<a class="section-link active" href="/dry-fit" aria-current="page">Dry fit</a>' in shown  # beside Models, in its category
    assert "Tick models in the list onto the cards" not in shown
    workspace = json.loads(re.search(r'<script type="application/json" id="fit-data">(.*?)</script>', shown, re.S)[1])
    strong = next(one for one in workspace['models'] if one['name'] == 'strong')
    assert strong['id'] == kept.identity('strong') and strong['points'] == [[4096, [5000]], [8192, [9000]]]
    assert strong['min'] == 4096 and strong['max'] == 8192
    assert [one['cards'] for one in strong['placements']] == [['A'], ['B']]
    assert next(one for one in workspace['models'] if one['name'] == 'new')['unknown']
    # Unknown device capacity is explicit, and system RAM remains visible on GPU nodes.
    assert workspace['resources'][0]['budget'] is None
    assert workspace['resources'][-1]['name'] == 'System RAM'
    assert not any(one['selected'] for one in workspace['models'])
    assert 'id="catalogue-search"' in shown and 'id="fit-table"' in shown
    assert shown.count("has to be loaded") == 4

    kept.put("processor", {"gpus": 0, "flags": {"model": gguf(tmp_path / "processor.gguf"), "ctx-size": 2048}})
    cpu_shown = browser.get("/dry-fit").text
    cpu_workspace = json.loads(re.search(r'<script type="application/json" id="fit-data">(.*?)</script>', cpu_shown, re.S)[1])
    cpu = next(one for one in cpu_workspace['models'] if one['name'] == 'processor')
    assert cpu['placements'] == [{'cards': [], 'label': 'System RAM'}] and cpu['ram_points']

    # A loaded model at half the quality preference cannot win an ordinary 25% bonus.
    # :available and :fast can still favor it; :best keeps the quality order.
    ticked = browser.get("/dry-fit", params={"part": "roles", "on": "quick"}).text
    assert "fit-table" not in ticked and "quick ✓" in ticked
    rows = {row.split("</code>")[0]: row for row in ticked.split("<code>")[1:]}
    assert all('<a href="/models/quick">quick</a></td><td>loaded' in rows[asked] for asked in ("chat:available", "chat:fast"))
    assert all('<a href="/models/strong">strong</a></td><td><span class="text-warning-emphasis">has to be loaded' in rows[asked] for asked in ('chat', 'chat:best'))
    roles.put('chat', {'models': {'quick': 1, 'strong': 2}, 'loaded_bonus':100})
    ticked = browser.get('/dry-fit', params={'part':'roles', 'on':'quick'}).text
    rows = {row.split('</code>')[0]:row for row in ticked.split('<code>')[1:]}
    assert '<a href="/models/quick">quick</a></td><td>loaded' in rows['chat']
    assert '100% loaded-model bonus' in rows['chat']
    unticked = browser.get('/dry-fit', params={'part':'roles'}).text
    rows = {row.split('</code>')[0]:row for row in unticked.split('<code>')[1:]}
    assert '<a href="/models/strong">strong</a>' in rows['chat']  # removes the hypothetical residency bonus


def test_dry_fit_fast_estimate_uses_hypothetical_residency_instead_of_the_live_load(desk, tmp_path):
    browser, switch, _ = desk
    models = Models(str(tmp_path / 'loadcoach.db'), {'flash-attn': 'on'})
    for name, seconds in (('longload', 100), ('shortload', 10)):
        models.put(name, {'gpus': 1, 'flags': {'model': gguf(tmp_path / (name + '.gguf')), 'ctx-size': 2048}})
        switch.front.state.log.add('model', 'loaded', model=name, seconds=seconds)
        switch.front.state.log.add('request', 'done', model=name, tokens_per_second=10, prompt_tokens_per_second=100)
    Roles(str(tmp_path / 'data' / 'roles.db')).put('chat', {'models': {'longload': 1, 'shortload': 1}})
    sign_in(browser, 'jordan')
    shown = browser.get('/dry-fit', params={'part': 'roles', 'on': 'longload'}).text
    row = next(row for row in shown.split('<code>') if row.startswith('chat:fast</code>'))
    assert '<a href="/models/longload">longload</a></td><td>loaded' in row
