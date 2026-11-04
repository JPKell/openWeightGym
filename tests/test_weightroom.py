"""weightroom's checks. A real LoadCoach app stands behind it, reached without a network, and a
switch between the two plays compute being off.
"""

import json
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

import spotter.app as spotter
from fake_hub import FakeHub
from weightroom.app import Settings, build_app, chart, load_settings, of_its_own, tail
from loadcoach.app import build_app as build_loadcoach
from loadcoach.config import Config
from loadcoach.log import Log
from loadcoach.models import Models
from loadcoach.runner import free_port
from loadcoach.groups import ABILITIES, OWN_KEY, Group, read_group
from loadcoach.users import Users, code_at
from front_helpers import Later, door, machine_key, own_copy, turnstile_to
from turnstile.app import Settings as TurnstileSettings, build_app as build_turnstile

GROUPS = {
    # Each ability by name, as a file would list them, and the most sensitive work: nobody hands out what they may not do themselves.
    "admin": Group(can=frozenset(ABILITIES) - OWN_KEY - {"superuser"}, max_sensitivity="restricted"),
    "plain": Group(can=frozenset({"load"})),
    "careful": Group(can=frozenset({"load"}), second_step=True),
    "waker": Group(can=frozenset({"load", "front-wake"})),
    "superuser": read_group("superuser", {"can": ["superuser"]}),
    "front": Group(can=frozenset({"sign-in", "mirror-users", "follow-settings"})),
}


def versioned_script(template: str, name: str) -> str:
    """Match the asset reference shipped by its page, without pinning a cache revision."""
    source = (Path(__file__).parents[1] / "weightroom" / "pages" / template).read_text()
    match = re.search(rf'<script src="/static/{re.escape(name)}\.js\?v=\d+" defer>', source)
    assert match, (template, name)
    return match.group()


class Switch(httpx.AsyncBaseTransport):
    """The wire between turnstile and a program on compute. With `off` set, nothing answers, as when compute is down."""

    def __init__(self, loadcoach) -> None:
        # The program sees these calls as coming from another machine, as turnstile's really do.
        self.wire, self.off = httpx.ASGITransport(app=loadcoach, client=("10.0.0.9", 1)), False

    async def handle_async_request(self, request):
        if self.off:
            raise httpx.ConnectError("compute is off")
        assert not request.url.path.startswith('/benchmarks'), 'Benchmark handlers must stay inside WeightRoom'
        return await self.wire.handle_async_request(request)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """weightroom and turnstile in front of a LoadCoach, with a few users. Yields the browser, the switch to compute, and the users."""
    monkeypatch.setattr("weightroom.app.LOOK_EVERY", 0.05)  # it looks at compute often, so a test need not wait long
    database = str(tmp_path / "loadcoach.db")
    (tmp_path / "data").mkdir()
    users = Users(str(tmp_path / "data" / "accounts.db"), GROUPS)  # the accounts are the front machine's, beside weightroom's data
    for name, group in (("jordan", "admin"), ("pat", "plain"), ("cleo", "careful"), ("wanda", "waker"), ("sam", "superuser"), ("panel", "front")):
        users.add(name, group)
        if name != "panel":
            users.change(name, password="correct horse")
    hub = FakeHub({"tiny-q4.gguf": os.urandom(1000), "big-00001-of-00002.gguf": b"one", "big-00002-of-00002.gguf": b"two"})  # what this LoadCoach takes for Hugging Face
    turnstile = Later()  # compute asks turnstile for the accounts, for its reports and for work done later in someone's name
    switch = Switch(build_loadcoach(Config(database=database, gpus=("A", "B"), model_defaults={"flash-attn": "on"}, nvidia_smi="no-such-program",
                                           machine_key_file=machine_key(tmp_path), front="http://front:8442"), hub.client(), door(turnstile, 8442)))
    switch.hub = hub
    front = switch.front = turnstile.app = turnstile_to(
        tmp_path, users, {8442: switch}, front_settings=True, toollocker_url="http://front-tools:9443")
    settings = Settings(compute="http://compute", key=users.new_key("panel"), data=str(tmp_path / "data"), wake_mac="bc:fc:e7:3d:90:6c", wake_address="10.0.0.255")
    with TestClient(build_app(settings, door(front, 8442))) as browser:
        yield browser, switch, users


def sign_in(browser, name, password="correct horse", **more):
    return browser.post("/sign-in", data={"name": name, "password": password, **more})


def test_background_refreshes_never_wake_compute_when_it_sleeps_between_reads(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from loadcoach import statement
    monkeypatch.setattr('weightroom.app.LOOK_EVERY', .01)
    monkeypatch.setattr('turnstile.app.TRY_EVERY', .001)
    data = tmp_path / 'data'; data.mkdir()
    users = Users(str(data / 'accounts.db'), GROUPS); users.add('panel', 'front')
    key_file = machine_key(tmp_path)
    async def status(request):
        return JSONResponse({'busy': 0, 'running': [], 'waiting': [], 'holds': [], 'loaded': [],
                             'cards': [], 'memory': {}})
    wire = httpx.ASGITransport(app=statement.Statements(Starlette(routes=[Route('/loadcoach/status', status)]), key_file))
    async def intermittent(request):
        # Status just answered, but the next background read meets a sleeping worker.
        if request.url.path == '/loadcoach/status':
            return await wire.handle_async_request(request)
        raise httpx.ConnectError('Compute went to sleep between reads.')
    wakes, requests = [], []
    front = build_turnstile(TurnstileSettings(compute='compute', data=str(data), machine_key_file=key_file,
        ports=(8442, 8444), wake_mac='bc:fc:e7:3d:90:6c', wait=.02),
        client=httpx.AsyncClient(transport=httpx.MockTransport(intermittent)),
        waker=lambda *args: wakes.append(args), users=users)
    async def observe(request):
        requests.append((request.url.path, request.headers.get('x-turnstile-no-wake')))
    coach, machine = door(front, 8442), door(front, 8444)
    for client in (coach, machine):
        client.event_hooks['request'].append(observe)
    settings = Settings(compute='http://compute', spotter='http://spotter', key=users.new_key('panel'), data=str(data))
    with TestClient(build_app(settings, coach, machine)):
        for _ in range(200):
            if sum(path == '/loadcoach/plugins/mirror' for path, _ in requests) >= 2:
                break
            time.sleep(.01)
        assert sum(path == '/loadcoach/plugins/mirror' for path, _ in requests) >= 2
        assert {'/loadcoach/status', '/loadcoach/plugins/mirror', '/loadcoach/alert-evidence', '/spotter/alert-evidence'} <= {path for path, _ in requests}
        assert all(header == '1' for _, header in requests), requests
        assert not wakes


def test_rename_from_users_page_and_case_insensitive_login(desk):
    browser, switch, users = desk
    assert '<h1 class="page-title wrap">Status</h1>' in sign_in(browser, "PaT").text
    pat_key = browser.cookies["weightroom"]
    sign_in(browser, "jordan")
    assert 'value="rename"' in browser.get("/users/pat").text
    for name in ("sam", "Has Space"):
        refused = browser.post("/users/pat", data={"do": "rename", "name": name})
        assert refused.status_code == 400 and users.exists("pat")
    renamed = browser.post("/users/pat", data={"do": "rename", "name": "alex"})
    assert renamed.status_code == 200 and renamed.url.path == "/users/alex"
    assert "Renamed pat." in renamed.text and users.by_key(pat_key).name == "alex"
    assert "Wrong name or password" in sign_in(browser, "PAT").text
    assert '<h1 class="page-title wrap">Status</h1>' in sign_in(browser, "AlEx").text
    assert browser.post("/users/alex", data={"do": "rename", "name": "other"}).status_code == 403
    sign_in(browser, "jordan")
    assert browser.post("/users/sam", data={"do": "rename", "name": "other"}).status_code == 400
    renamed = browser.post("/users/jordan", data={"do": "rename", "name": "admin2"})
    assert renamed.status_code == 200 and '<h1 class="page-title wrap">Status</h1>' in browser.get("/").text

    # With compute off the accounts are still here, with the same spelling and the same count of wrong tries.
    switch.off = True
    assert "Node unavailable: compute" in sign_in(browser, "WaNdA").text and browser.cookies["weightroom"].startswith("lc_")
    for name in ("WANDA", "wanda", "Wanda", "wAnda", "waNda"):
        assert "Wrong name or password" in sign_in(browser, name, "wrong").text
    assert "Too many wrong tries" in sign_in(browser, "WANda").text


def test_signing_in_is_checked_by_loadcoach_and_the_browser_gets_a_session(desk, tmp_path):
    browser, _, users = desk
    assert "Sign in" in browser.get("/").text  # nobody yet, so every page leads here
    background = browser.get('/strip', headers={'HX-Request': 'true'}, follow_redirects=False)
    assert background.status_code == 204 and background.headers['HX-Redirect'] == '/sign-in'
    assert background.headers['cache-control'] == 'no-store'
    assert not background.content  # no autofocus login form inside the telemetry fragment
    regular = browser.get('/strip', follow_redirects=False)
    assert regular.status_code == 303 and regular.headers['location'] == '/sign-in'

    wrong = sign_in(browser, "pat", "wrong horse")
    assert wrong.status_code == 401 and "Wrong name or password." in wrong.text
    home = sign_in(browser, "pat")
    assert home.status_code == 200 and '<h1 class="page-title wrap">Status</h1>' in home.text and '>System</a>' in home.text
    # The categories stay in view. Section pages have a collapsible rail; the name opens the account menu.
    assert '<header class="sticky-top bg-body">' in home.text and 'pat</summary>' in home.text
    assert '<a class="dropdown-item" href="/account">My account</a>' in home.text and '<button class="dropdown-item">Sign out</button>' in home.text
    assert 'aria-controls="site-sidebar"' in home.text and home.text.count('<a class="dropdown-item" href="/account">') == 1  # the account is reached from the name, and from nowhere else
    key = browser.cookies["weightroom"]
    assert users.by_key(key).name == "pat" and users.by_key(key).until  # the cookie is pat's own session key, and it ends

    log = Log(str(tmp_path / "data" / "accounts.db")).read(user="pat")  # on record at the front machine
    assert [event["what"] for event in log] == ["sign-in", "signed-in"] and log[-1]["at_front_from"] == "testclient"

    assert "Sign in" in browser.post("/sign-out").text
    assert users.by_key(key) is None and "weightroom" not in browser.cookies  # the key itself stopped working, not only the cookie


def test_a_first_sign_in_chooses_a_password_and_sets_up_a_code_on_the_same_page(desk):
    browser, _, users = desk
    temporary = users.temporary_password("pat")
    asked = sign_in(browser, "pat", temporary)
    assert "A password of your own" in asked.text and f'name="password" value="{temporary}"' in asked.text
    unequal = sign_in(browser, "pat", temporary, new_password="battery staple", new_again="battery stable")
    assert "not the same" in unequal.text
    assert '<h1 class="page-title wrap">Status</h1>' in sign_in(browser, "pat", temporary, new_password="battery staple", new_again="battery staple").text
    browser.post("/sign-out")

    setup = sign_in(browser, "cleo")
    secret = re.search(r'<pre class="secret">(\w+)</pre>', setup.text).group(1)
    assert "authenticator app" in setup.text
    assert '<h1 class="page-title wrap">Status</h1>' in sign_in(browser, "cleo", code=code_at(secret, int(time.time() // 30))).text
    browser.post("/sign-out")
    assert "Code from your authenticator app" in sign_in(browser, "cleo").text and "secret" not in sign_in(browser, "cleo").text


def test_my_account_shows_and_changes_what_is_ones_own(desk):
    browser, _, users = desk
    sign_in(browser, "pat")
    account = browser.get("/account").text
    assert "plain" in account and "No keys." in account and "1 place" in account

    made = browser.post("/account/keys", data={"label": "<b>laptop</b>"})
    key = re.search(r'<pre class="secret">(lc_[\w-]+)</pre>', made.text).group(1)
    assert users.by_key(key).name == "pat"
    assert "&lt;b&gt;laptop&lt;/b&gt;" in made.text and "<b>laptop</b>" not in made.text  # what a person typed is never run as HTML
    key_id = users.listing("pat")[0]["keys"][0]["id"]
    assert key not in browser.get("/account").text  # shown once
    assert "Key removed." in browser.post(f"/account/keys/{key_id}/remove").text and users.by_key(key) is None

    refused = browser.post("/account/password", data={"current_password": "wrong", "new_password": "another one", "new_again": "another one"})
    assert refused.status_code == 400 and "current password" in refused.text
    changed = browser.post("/account/password", data={"current_password": "correct horse", "new_password": "another one", "new_again": "another one"})
    assert "Password changed." in changed.text and '<h1 class="page-title wrap">Status</h1>' in browser.get("/").text  # still signed in here
    browser.post("/sign-out")
    assert "Wrong name" in sign_in(browser, "pat").text and '<h1 class="page-title wrap">Status</h1>' in sign_in(browser, "pat", "another one").text


def test_every_way_to_compute_goes_through_turnstile_on_this_machine(tmp_path):
    """Compute believes only what turnstile signs, so nothing on the page side may ask compute straight, benchmarks included."""
    (tmp_path / "weightroom.key").write_text("k")
    (tmp_path / "weightroom.toml").write_text('compute = "http://10.0.0.4:8442"\nkey_file = "weightroom.key"\nspotter = "http://10.0.0.4:8444"\n[turnstile]\nhost = "0.0.0.0"\n')
    app = build_app(load_settings(tmp_path / "weightroom.toml"))
    worker = app.state.benchmarks.state.worker.client
    assert str(worker.base_url).startswith("http://127.0.0.1:8442") and worker.headers["x-turnstile-no-wake"] == "1"


def test_with_compute_off_it_says_so_and_anyone_may_sign_in_since_the_accounts_are_here(desk, monkeypatch):
    browser, switch, _ = desk
    woken = []
    monkeypatch.setattr("weightroom.app.wake", lambda *sent: woken.append(sent))  # no packet leaves this machine
    sign_in(browser, "pat")
    session = browser.cookies["weightroom"]

    switch.off = True
    off = browser.get("/")
    assert off.status_code == 200 and "Node unavailable: compute" in off.text  # the first page, with the last it saw
    assert "compute off<span class=\"reading\"> since" in browser.get("/pulse").text
    assert browser.get("/models?complete=1").status_code == 403  # permissions are checked before the compute lookup
    assert browser.cookies["weightroom"] == session  # the sign-in is kept for when compute is back
    switch.off = False
    assert '<h1 class="page-title wrap">Status</h1>' in browser.get("/").text and "<b>compute</b>" in browser.get("/pulse").text
    assert "Your group may not wake compute." in browser.post("/wake").text and woken == []

    # Off again. Turnstile checks a password on this machine, so anyone may sign in, and see what compute last did.
    switch.off = True
    assert "Wrong name or password" in sign_in(browser, "wanda", "wrong horse").text
    assert "Node unavailable: compute" in sign_in(browser, "pat").text and '<form method="post" action="/wake"' not in browser.get("/").text
    home = sign_in(browser, "wanda")
    assert "Node unavailable: compute" in home.text and '<h1 class="page-title wrap">Status</h1>' in home.text
    # Waking compute is the one thing to do while it is off, for those whose group may.
    assert '<form method="post" action="/wake"' in home.text and woken == []
    assert "Asked compute to wake." in browser.post("/wake").text and woken == [("bc:fc:e7:3d:90:6c", "10.0.0.255")]

    # A superuser may do everything, which includes what the front machine itself lets people do.
    assert "Asked compute to wake." in (sign_in(browser, "sam") and browser.post("/wake")).text and len(woken) == 2

    # A cookie that is no session key is nobody.
    browser.cookies.clear()
    assert "Sign in" in browser.get("/", headers={"cookie": "weightroom=lc_made-up"}).text
    assert "Sign in" in browser.post("/wake", headers={"cookie": "weightroom=lc_made-up"}).text and len(woken) == 2  # and nobody wakes nothing


def test_front_owned_pages_remain_available_when_every_loadcoach_is_down(desk):
    browser, switch, _ = desk
    sign_in(browser, "jordan")
    switch.front.state.routing.reliability.record(
        999, "jordan", "saved-model", ("saved-id", "saved-fingerprint", ""), 0, "success")
    store = browser.app.state.benchmarks.state.store
    number = store.create("jordan", "saved-model", "load")
    store.activate(number, {"fingerprint": "saved-fingerprint", "subject": {"values": {}},
                            "environment": {"engine": "saved", "engine_hash": "saved", "machine": {}}})
    store.keep(number, "load-1", {"context_tokens": 2048, "success": True, "load_seconds": 2.0}, "", "")
    store.finish(number, "completed")
    store.synced(number)
    switch.off = True
    paths = (
        "/", "/account", "/account/appearance", "/account/logs", "/chat", "/collections", "/workflows",
        "/benchmarks", "/benchmarks/runs", "/benchmarks/embedders", "/benchmarks/results",
        "/benchmarks/compare", "/benchmarks/manage", f"/benchmarks/run/{number}", "/usage", "/requests?completed=1", "/batches", "/log", "/reliability",
        "/status", "/telemetry", "/models", "/compute-nodes", "/chat/images", "/chat/music", "/chat/sounds", "/chat/speech", "/alerts", "/schedules", "/users", "/groups", "/tools",
        "/plugins", "/plugins/activity", "/backups", "/databases", "/settings", "/docs",
    )
    unavailable = [(path, response.status_code) for path in paths if (response := browser.get(path)).status_code != 200]
    assert not unavailable, unavailable
    assert "Saved benchmark results remain available" in browser.get("/benchmarks").text
    assert "saved-model" in browser.get(f"/benchmarks/run/{number}").text
    reliability = browser.get("/reliability").text
    assert "Showing saved reliability evidence" in reliability and "saved-model" in reliability


def test_a_form_sent_from_another_site_is_refused(desk):
    browser, _, _ = desk
    elsewhere = {"origin": "http://evil.example"}
    assert browser.post("/sign-in", data={"name": "pat", "password": "correct horse"}, headers=elsewhere).status_code == 403
    sign_in(browser, "pat")
    assert browser.post("/account/keys", data={"label": "x"}, headers=elsewhere).status_code == 403
    assert browser.post("/account/keys", data={"label": "x"}, headers={"origin": "http://testserver"}).status_code == 200


def test_the_settings_file_names_where_the_key_is(tmp_path):
    (tmp_path / "weightroom.key").write_text("lc_the_key\n")
    file = tmp_path / "weightroom.toml"
    file.write_text('compute = "http://10.77.10.84:8442"\nkey_file = "weightroom.key"\nhost = "0.0.0.0"\ndata = "kept"\n')
    settings = load_settings(file)
    assert (settings.compute, settings.key, settings.host, settings.port) == ("http://10.77.10.84:8442", "lc_the_key", "0.0.0.0", 8440)
    assert (settings.mock_input_rate, settings.mock_output_rate) == (2.0, 10.0)
    assert settings.third_party_services == ("ollama", "docker")
    assert settings.data == str(tmp_path / "kept") and not settings.secure
    # turnstile reads the same file. Its own part is not weightroom's, and what wakes compute is shared.
    file.write_text('compute = "http://c:1"\nkey_file = "weightroom.key"\nwake_mac = "bc:fc:e7:3d:90:6c"\n[turnstile]\nports = [8442]\n')
    assert (load_settings(file).wake_mac, load_settings(file).wake_address) == ("bc:fc:e7:3d:90:6c", "255.255.255.255")
    # So is turnstile's inventory of further compute machines: a front that lists them must still start.
    file.write_text('compute = "http://c:1"\nkey_file = "weightroom.key"\nmachines_poll_seconds = 5\n[machines.cpu]\ncompute = "http://127.0.0.1:9442"\nmachine_key_file = "cpu.key"\n')
    assert load_settings(file).compute == "http://c:1"

    for wrong in ('compute = "http://x"\n', 'compute = "http://x"\nkey_file = "nowhere"\n', 'key_file = "weightroom.key"\n', 'compute = "x"\nkey_file = "weightroom.key"\ncolour = 1\n', 'compute = "http://x"\nkey_file = "weightroom.key"\nmock_input_rate = -1\n', 'compute = "http://x"\nkey_file = "weightroom.key"\nthird_party_services = ["docker", "other"]\n'):
        file.write_text(wrong)
        with pytest.raises(SystemExit):
            load_settings(file)
    static = Path(__file__).parent.parent / "weightroom/static"  # the two files that are not ours, exactly as fetched
    assert (static / "htmx.min.js").stat().st_size == 50917 and "Tabler v1.4.0" in (static / "tabler.min.css").read_text()


DISABLED = "<fieldset disabled>"  # what a page wraps its forms in when LoadCoach would refuse them all


def test_ticks_become_what_an_account_is_given_and_what_is_taken_from_it():
    known = {"abilities": ABILITIES, "own_key": sorted(OWN_KEY)}
    others = sorted(set(ABILITIES) - OWN_KEY - {"superuser"})
    assert of_its_own(["load", "lock"], ["load", "hosted"], known) == {"add": ["lock"], "remove": ["hosted"]}
    assert of_its_own(["load"], ["load"], known) == {"add": [], "remove": []}
    # The one word is given, not everything it stands for. Ticked on an account whose group has it, nothing differs.
    assert of_its_own(["superuser", *others, "front-wake", "sign-in"], ["load"], known) == {"add": ["sign-in", "superuser"], "remove": []}
    assert of_its_own(["superuser", *others, "front-wake"], ["superuser"], known) == {"add": [], "remove": []}
    # Unticked on such an account, the word is taken away, and what the front machine read into it is given by name.
    assert of_its_own([*others, "front-wake"], ["superuser"], known) == {"add": ["front-wake"], "remove": ["superuser"]}
    assert of_its_own([one for one in others if one != "power"], ["superuser"], known) == {"add": [], "remove": ["power", "superuser"]}


def test_the_users_page_adds_an_account_and_its_own_page_changes_it(desk):
    browser, _, users = desk
    assert 'href="/users"' not in sign_in(browser, "pat").text and browser.get("/users").status_code == 403
    browser.post("/sign-out")
    sign_in(browser, 'jordan')
    assert 'href="/users"' in browser.get('/users').text
    listed = browser.get("/users").text
    assert '<a href="/users/pat">pat</a>' in listed and "<td>careful</td>" in listed
    first = browser.get("/users?limit=2&page=1").text
    second = browser.get("/users?limit=2&page=2").text
    first_names = re.findall(r'<a href="/users/([^"/]+)">', first)
    second_names = re.findall(r'<a href="/users/([^"/]+)">', second)
    assert len(first_names) == len(second_names) == 2 and set(first_names).isdisjoint(second_names)
    assert 'id="users-limit"' in first and '<option value="2" selected>2</option>' in first
    assert 'class="d-flex flex-nowrap align-items-center gap-3 mb-2"' in first
    assert "6 results · Page 1 of 3" in first and 'href="/users?limit=2&amp;page=2"' in first
    assert "6 results · Page 3 of 3" in browser.get("/users?limit=2&page=999").text

    # Added for a person: a temporary password and no key, shown this once on the account's own page.
    wrong = browser.post("/users", data={"name": "Ada L", "group": "plain", "password": "on"})
    assert wrong.status_code == 400 and "a user name is lowercase letters" in wrong.text and 'value="Ada L"' in wrong.text
    added = browser.post("/users", data={"name": "ada", "group": "plain", "password": "on"})
    secret = re.findall(r'<pre class="secret">(.+?)</pre>', added.text)
    assert "Added ada." in added.text and "Temporary password." in added.text and len(secret) == 1 and "API key." not in added.text
    assert (users.listing("ada")[0]["password"], users.listing("ada")[0]["keys"]) == ("temporary", []) and secret[0] not in browser.get("/users/ada").text

    # Its page: what it asked for over three spans, the last of which can be chosen.
    page = browser.get("/users/ada").text
    assert all(span in page for span in ("<td>24 hours</td>", "<td>7 days</td>", "<td>28 days</td>")) and "<td>3 days</td>" in browser.get("/users/ada?days=3").text
    assert '<h2 class="section-title mb-0">Requests</h2>' in page and "Asked for" not in page
    assert page.index('<h2 class="section-title mb-0">Rename</h2>') < page.index('<h2 class="section-title mb-0">Requests</h2>')
    assert page.index('<h2 class="section-title mb-0">API keys</h2>') < page.index('<h2 class="section-title mb-0">Requests</h2>') and page.count('class="col-lg-6"') == 2
    assert page.index('value="enabled-no"') < page.index('<dl class="row facts')
    assert '<summary class="card-header fw-semibold">Remove</summary>' not in page and '<h2 class="section-title mb-0">Remove</h2>' in page
    assert "Keys, sign-ins and saved conversations stay" not in page and "For programs that use the models" not in page
    # A tick for every ability: its group's are ticked and marked, and one the person looking may not do is greyed.
    box = lambda name, text: re.search(rf'<input id="can-{name}"[^>]*>', text).group(0)  # noqa: E731
    assert "checked" in box("load", page) and "checked" not in box("lock", page) and "disabled" in box("superuser", page) and "disabled" not in box("lock", page)
    assert DISABLED not in page

    # Ticks are turned into what it is given and what is taken away. Ticked as its group is, it has nothing of its own again.
    saved = browser.post("/users/ada", data={"do": "abilities", "can": ["lock", "see-log"]})
    assert "Saved." in saved.text and users.own("ada") == {"add": ["lock", "see-log"], "remove": ["load"]} and "differs from its group" in browser.get("/users").text
    assert re.search(r'<details class="card mb-3" open>.*?class="card-heading">Abilities</span>', saved.text, re.S)  # the card just used is left open
    assert saved.text.count(">given</span>") == 2 and saved.text.count(">taken away</span>") == 1  # and what differs from its group is pointed at
    browser.post("/users/ada", data={"do": "abilities", "can": ["load"]})
    assert users.own("ada") == {} and "differs from its group" not in browser.get("/users").text

    # Tighter than its group, and what LoadCoach refuses is said in its words.
    browser.post("/users/ada", data={"do": "limits", "fewer": "on", "model": ["small"], "max_requests": "2", "max_sensitivity": "normal", "priority": "high"})
    assert users.own("ada") == {"models": ["small"], "max_requests": 2, "max_sensitivity": "normal", "priority": "high"} and "<option selected>high</option>" in browser.get("/users/ada").text
    assert users.by_name("ada").group.priority == "high" and 'value="small" type="checkbox" class="form-check-input" checked' in browser.get("/users/ada").text
    refused = browser.post("/users/ada", data={"do": "limits", "max_requests": "0"})
    assert refused.status_code == 400 and "max_requests must be a whole number, 1 or more." in refused.text
    browser.post("/users/ada", data={"do": "limits"})
    assert users.own("ada") == {}

    # Its group, its password, its keys, and whether it is enabled.
    browser.post("/users/ada", data={"do": "group", "group": "careful"})
    assert users.listing("ada")[0]["group"] == "careful" and "No group named" in browser.post("/users/ada", data={"do": "group", "group": "nobody"}).text
    again = browser.post("/users/ada", data={"do": "password-new"})
    assert "Temporary password." in again.text and re.search(r'<pre class="secret">(.+?)</pre>', again.text).group(1) != secret[0]
    made = browser.post("/users/ada", data={"do": "keys-add", "label": "laptop"})
    key = re.search(r'<pre class="secret">(lc_[\w-]+)</pre>', made.text).group(1)
    assert users.by_key(key).name == "ada" and "laptop" in made.text
    browser.post("/users/ada", data={"do": "enabled-no"})
    assert users.by_key(key) is None and "disabled</span>" in browser.get("/users/ada").text
    browser.post("/users/ada", data={"do": "enabled-yes"})
    browser.post("/users/ada", data={"do": "keys-remove", "key": users.listing("ada")[0]["keys"][0]["id"]})
    assert users.listing("ada")[0]["enabled"] and users.by_key(key) is None and "No keys." in browser.get("/users/ada").text

    # One's own account, and one that may do more, are shown and cannot be changed. Sent anyway, LoadCoach refuses.
    mine, above = browser.get("/users/jordan").text, browser.get("/users/sam").text
    assert DISABLED in mine and "with the weightroom command" in mine and 'href="/account"' in mine
    assert DISABLED in above and "sam may do more than you" in above
    forced = browser.post("/users/sam", data={"do": "password-new"})
    assert forced.status_code == 400 and "sam may do more than you" in forced.text and users.listing("sam")[0]["password"] is True

    removed = browser.post("/users/ada", data={"do": "remove"})
    assert "Removed ada." in removed.text and str(removed.url).split("?")[0].endswith("/users") and not users.exists("ada")
    assert browser.get("/users/ada").status_code == 404

    # Someone who can do everything hands out anything, and the one word for it is what is kept.
    browser.post("/sign-out")
    sign_in(browser, "sam")
    assert "disabled" not in box("superuser", browser.get("/users/pat").text)
    # Their own account is theirs to change too, all but disabling and removing it.
    mine = browser.get("/users/sam").text
    assert DISABLED not in mine and 'value="limits"' in mine and 'value="enabled-no"' not in mine and 'value="remove"' not in mine
    browser.post("/users/sam", data={"do": "limits", "priority": "high"})
    assert users.own("sam") == {"priority": "high"} and browser.post("/users/sam", data={"do": "enabled-no"}).status_code == 400 and users.listing("sam")[0]["enabled"]
    browser.post("/users/pat", data={"do": "abilities", "can": ["superuser", "load", "power"]})
    assert users.own("pat") == {"add": ["superuser"]} and "power" in users.listing("pat")[0]["can"]
    assert "checked" in box("front-wake", browser.get("/users/pat").text)  # the front machine's own abilities come with the word


def test_the_groups_page_adds_a_group_and_its_own_page_changes_it(desk):
    browser, _, users = desk
    assert 'href="/groups"' not in sign_in(browser, "pat").text and browser.get("/groups").status_code == 403
    browser.post("/sign-out")
    sign_in(browser, 'jordan')
    assert 'href="/groups"' in browser.get('/groups').text
    listed = browser.get("/groups").text
    assert '<a href="/groups/plain">plain</a>' in listed and "everything" in listed and "every model" in listed
    first = browser.get("/groups?limit=2&page=1").text
    second = browser.get("/groups?limit=2&page=2").text
    first_names = re.findall(r'<a href="/groups/([^"/]+)">', first)
    second_names = re.findall(r'<a href="/groups/([^"/]+)">', second)
    assert len(first_names) == len(second_names) == 2 and set(first_names).isdisjoint(second_names)
    assert 'id="groups-limit"' in first and '<option value="2" selected>2</option>' in first
    assert "6 results · Page 1 of 3" in first and 'href="/groups?limit=2&amp;page=2"' in first
    assert "6 results · Page 3 of 3" in browser.get("/groups?limit=2&page=999").text

    wrong = browser.post("/groups", data={"name": "Read ers"})
    assert wrong.status_code == 400 and "A group name is lowercase letters" in wrong.text and 'value="Read ers"' in wrong.text
    added = browser.post("/groups", data={"name": "readers"})
    assert str(added.url).split("?")[0].endswith("/groups/readers") and "It allows nothing yet" in added.text and users.groups.stored("readers") == {}
    assert "already a group named" in browser.post("/groups", data={"name": "readers"}).text

    # Every setting is saved as one thing.
    form = {"do": "save", "can": ["load", "lock"], "model": ["small", "big"], "priority": "low", "max_sensitivity": "sensitive", "max_requests": "2", "max_lock": "60",
            "max_hosted_spend": "1.5", "session_hours": "8", "second_step": "on"}  # fmt: skip
    saved = browser.post("/groups/readers", data=form)
    assert "Saved." in saved.text and users.groups.stored("readers") == {
        "can": ["load", "lock"], "models": ["small", "big"], "priority": "low", "max_sensitivity": "sensitive", "max_requests": 2, "max_lock": 60,
        "max_hosted_spend": 1.5, "session_hours": 8, "second_step": True}  # fmt: skip
    assert 'name="max_hosted_spend" type="number" min="0" step="any" class="form-control" value="1.5"' in saved.text and 'value="big" type="checkbox" class="form-check-input" checked' in saved.text
    page = browser.get("/groups/readers").text
    assert '<summary class="card-header fw-semibold">' not in page
    assert page.index(">Members</h2>") < page.index(">Limits and signing in</h2>") < page.index(">What members may do</h2>")
    assert page.count('class="col-md-6 col-xl-3') == 8 and 'name="do" value="limits"' in page and 'name="do" value="abilities"' in page
    assert "in seconds" not in page and "for each member, in line or being answered" not in page
    assert 'id="move-group"' in page and 'name="do" value="move-members"' in page

    # Each of the two Save buttons changes its own section and preserves the other one.
    limits = browser.post("/groups/readers", data={"do": "limits", "priority": "high", "max_sensitivity": "normal", "max_lock": "30", "max_hold": "90",
                                                       "max_requests": "4", "max_hosted_spend": "2", "session_hours": "6"})
    assert "Saved." in limits.text and users.groups.stored("readers") == {
        "can": ["load", "lock"], "models": ["small", "big"], "priority": "high", "max_sensitivity": "normal", "max_requests": 4,
        "max_lock": 30, "max_hold": 90, "max_hosted_spend": 2, "session_hours": 6, "second_step": False}  # fmt: skip
    abilities = browser.post("/groups/readers", data={"do": "abilities", "can": ["load"], "every": "on"})
    assert "Saved." in abilities.text and users.groups.stored("readers")["models"] == "*" and users.groups.stored("readers")["max_requests"] == 4
    # What LoadCoach refuses is said in its words, and what was typed stays in the form.
    refused = browser.post("/groups/readers", data={**form, "max_requests": "-1", "every": "on"})
    assert refused.status_code == 400 and "must be 0 or more" in refused.text and 'value="-1"' in refused.text and users.groups.stored("readers")["max_requests"] == 4
    too_much = browser.post("/groups/readers", data={**form, "can": ["load", "superuser"]})
    assert too_much.status_code == 400 and "which you may not do yourself" in too_much.text

    # One's own group, and one that allows more, are shown and cannot be changed.
    mine, above = browser.get("/groups/admin").text, browser.get("/groups/superuser").text
    assert DISABLED in mine and "Nobody changes the group they are in" in mine and '<a href="/users/jordan">jordan</a>' in mine
    assert DISABLED in above and "allows more than you may" in above and DISABLED not in browser.get("/groups/readers").text

    # Every member can be moved together, after which the empty group can be removed.
    users.add("ada", "readers")
    in_use = browser.post("/groups/readers", data={"do": "remove"})
    assert in_use.status_code == 400 and "Move them to another group first" in in_use.text
    users.add("bob", "readers")
    moved = browser.post("/groups/readers", data={"do": "move-members", "group": "plain"})
    assert "Moved 2 members to plain." in moved.text and users.listing("ada")[0]["group"] == users.listing("bob")[0]["group"] == "plain"
    removed = browser.post("/groups/readers", data={"do": "remove"})
    assert "Removed readers." in removed.text and "readers" not in users.groups and browser.get("/groups/readers").status_code == 404

    # "superuser" ticked is kept as the one word, whatever else is ticked beside it.
    browser.post("/sign-out")
    sign_in(browser, "sam")
    browser.post("/groups", data={"name": "owners"})
    browser.post("/groups/owners", data={"do": "save", "can": ["superuser", "load", "power", "front-wake", "sign-in"], "every": "on", "priority": "normal", "max_sensitivity": "restricted"})
    assert users.groups.stored("owners")["can"] == ["sign-in", "superuser"] and users.groups.stored("owners")["models"] == "*"


def test_the_models_page_is_for_those_who_may_manage_models(desk):
    browser, _, _ = desk
    assert 'href="/models"' not in sign_in(browser, "pat").text  # no link to a page that would refuse
    refused = browser.get("/models?complete=1")
    assert refused.status_code == 403 and "may not add, change and remove models" in refused.text
    browser.post("/sign-out")
    assert 'href="/models"' in sign_in(browser, "jordan").text and "No models yet." in browser.get("/models?complete=1").text


@pytest.mark.parametrize("cards, expected", [(None, "any"), ("", "any"), ("   ", "any"), ("any", "any"), ("0", "any"), ("1", "any"), ("all", "any")])
def test_new_model_defaults_to_any_card(desk, tmp_path, cards, expected):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    file = tmp_path / "default.gguf"
    file.touch()
    for url in ("/models", f"/models?file={file}"):
        assert 'name="cards"' not in browser.get(url).text and 'id="allow-split"' not in browser.get(url).text
    form = {"name": "default", "file": str(file), "ctx": "2048"}
    if cards is not None:
        form["cards"] = cards
    added = browser.post("/models", data=form)
    assert added.status_code == 200 and "Added default." in added.text
    assert Models(str(tmp_path / "loadcoach.db")).stored("default").get("gpus") == expected
    assert Models(str(tmp_path / "loadcoach.db")).stored("default")["allow_split"] is True


def test_a_model_is_added_edited_and_removed_from_its_one_page(desk, tmp_path):
    browser, _, _ = desk
    kept = Models(str(tmp_path / "loadcoach.db"))
    file = tmp_path / "small.gguf"
    file.write_bytes(b"x" * 3 * 2**20)
    sign_in(browser, "jordan")

    # Added from the list page. A name may hold a slash and a colon.
    assert "needs both" in browser.post("/models", data={"name": "half", "file": str(file), "ctx": ""}).text
    added = browser.post("/models", data={"name": "org/small:v1", "file": str(file), "ctx": "4096", "cards": "1"})
    assert str(added.url).endswith("/models/org/small:v1?said=Added%20org/small%3Av1.%20It%20can%20be%20asked%20for%20from%20now%20on.")
    assert "Added org/small:v1." in added.text and "Last used</span> never" in added.text and "3 MB" in added.text
    assert "could not be read as a model file" in added.text  # three megabytes of x is no model, and the page says so
    assert kept.stored("org/small:v1") == {"gpus": "any", "allow_split":True, "flags": {"model": str(file), "ctx-size": 4096}}
    again = browser.post("/models", data={"name": "org/small:v1", "file": str(file), "ctx": "1", "cards": ""})
    assert again.status_code == 400 and "already a model named org/small:v1" in again.text and f'value="{file}"' in again.text
    assert kept.stored("org/small:v1")["flags"]["ctx-size"] == 4096  # and the one that was there is untouched

    # Seed a legacy placement for the remaining editor coverage; creation uses automatic placement.
    kept.put("org/small:v1", {"gpus":1,"flags":{"model":str(file),"ctx-size":4096}})
    # Its page shows what is kept, and the defaults every model gets.
    its = browser.get("/models/org/small:v1").text
    assert 'name="ctx" class="form-control" value="4096"' in its and 'name="placement_controls" value="yes"' in its and "flash-attn = on" in its
    assert 'name="gpu" value="yes" type="checkbox" class="form-check-input" data-placement-device="gpu" aria-controls="gpu-placement" checked' in its
    assert 'name="cards"' not in its and 'name="cpu"' in its and 'name="gpu_card" value="A"' in its and 'name="gpu_card" value="B"' in its
    assert "Manage GPUs in Compute nodes" not in its

    # A model on a card is offered the flag that puts all of it there. Ticked and saved, it is set, and offered no more.
    assert 'name="suggested" value="gpu-layers=all"' in its and "tensor-split=" not in its  # one card: nothing to divide
    ticked = browser.post("/models/org/small:v1", data={"file": str(file), "ctx": "4096", "cards": "", "suggested": ["gpu-layers=all", "tensor-split=[1, 1]"], "do": "save"})
    assert kept.stored("org/small:v1")["flags"] == {"model": str(file), "ctx-size": 4096, "gpu-layers": "all", "tensor-split": [1, 1]} and 'name="suggested"' not in ticked.text
    # On both cards, and without them: the division over the cards is offered too. On the processor alone, nothing is.
    both = browser.post("/models/org/small:v1", data={"file": str(file), "ctx": "4096", "cards": "", "do": "save"}).text
    assert 'value="gpu-layers=all"' in both and 'value="tensor-split=[1, 1]"' in both and "evenly over its 2 cards" in both
    assert 'name="suggested"' not in browser.post("/models/org/small:v1", data={"file": str(file), "ctx": "4096", "cards": "0", "do": "save"}).text
    browser.post("/models/org/small:v1", data={"file": str(file), "ctx": "4096", "cards": "1", "do": "save"})

    # Edited: rows of flag and value. A leading dash is dropped, a row with no value takes the flag away.
    form = {"file": str(file), "ctx": "8192", "placement_controls": "yes", "gpu": "yes", "gpu_card": ["A", "B"], "allow_split": "yes",
            "flag": ["parallel", "temp", "--top-k", "tensor-split", "embedding", "gone", ""],
            "value": ["2", "0.7", "20", "[1, 1]", "true", "", ""], "do": "save"}  # fmt: skip
    saved = browser.post("/models/org/small:v1", data=form)
    assert "Saved." in saved.text
    assert kept.stored("org/small:v1") == {"gpus": ["A", "B"], "allow_split": True, "flags": {
        "model": str(file), "ctx-size": 8192, "parallel": 2, "temp": 0.7, "top-k": 20, "tensor-split": [1, 1], "embedding": True}}  # fmt: skip
    assert 'name="gpu_card" value="A" type="checkbox" class="form-check-input" data-gpu-card checked' in saved.text
    assert 'name="gpu_card" value="B" type="checkbox" class="form-check-input" data-gpu-card checked' in saved.text
    assert 'value="[1, 1]"' in saved.text and 'value="true"' in saved.text
    assert 'value="tensor-split" aria-label="flag" readonly' in saved.text

    # What LoadCoach refuses is said in its words, and what was typed stays in the form.
    refused = browser.post("/models/org/small:v1", data={**form, "gpu_card": "C", "ctx": "16384"})
    assert refused.status_code == 400 and "gpus must be" in refused.text and 'name="ctx" class="form-control" value="16384"' in refused.text
    assert kept.stored("org/small:v1")["flags"]["ctx-size"] == 8192

    # The list, and the piece of it that the page asks for again by itself.
    kept.put("lost", {"gpus": 0, "flags": {"model": "/no/such/file.gguf", "ctx-size": 1}})
    whole, part = browser.get("/models?complete=1").text, browser.get("/models?part=list").text
    assert "<html" in whole and "<html" not in part and 'hx-get="/models?part=list"' in part
    assert '<a href="/models/org/small:v1">org/small:v1</a>' in part and "8,192" in part and "its file is missing" in part
    state = browser.get("/models/org/small:v1?part=state").text
    assert "<html" not in state and "Last used" in state and str(file) in state and "Fingerprint" not in state

    assert browser.get("/models/nobody").status_code == 404
    # Renamed from among its settings, by a form of its own. A name that is taken is refused, with the reason shown.
    taken = browser.post("/models/org/small:v1", data={"do": "rename", "to": "lost"})
    assert "There is already a model named" in taken.text and '<input id="to" name="to" form="rename"' in taken.text and '<button form="rename"' in taken.text
    renamed = browser.post("/models/org/small:v1", data={"do": "rename", "to": "small-v1"})
    assert str(renamed.url).split("?")[0].endswith("/models/small-v1") and "Renamed org/small:v1 to small-v1." in renamed.text
    assert kept.names() == ["lost", "small-v1"] and browser.get("/models/org/small:v1").status_code == 404

    removed = browser.post("/models/small-v1", data={"do": "remove"})
    assert "Removed small-v1. Its file is left where it is." in removed.text and str(removed.url).split("?")[0].endswith("/models")
    assert kept.stored("org/small:v1") is None and file.exists()


def gguf(path, **told):
    """Write a small file laid out as a .gguf begins: its facts, and no weights."""
    text = lambda value: struct.pack("<Q", len(value.encode())) + value.encode()  # noqa: E731
    fact = lambda key, value: text(key) + (struct.pack("<I", 8) + text(value) if isinstance(value, str) else struct.pack("<IQ", 10, value))  # noqa: E731
    Path(path).write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(told)) + b"".join(fact(key.replace("__", "."), value) for key, value in told.items()))
    return str(path)


def test_a_model_page_lists_every_flag_llama_server_knows(live_desk):
    browser, _, _ = live_desk
    sign_in(browser, "jordan")
    page = browser.get("/models/small").text
    assert page.index('class="card-heading">Compute locations</span>') < page.index('class="card-heading">Benchmark results</span>')
    listed = page.split('<table id="every-flag"')[1].split("</table>")[0]
    assert "Every flag this llama-server knows" in page and "<code>flash-attn</code>" in listed and "<code>[on|off|auto]</code>" in listed
    assert "<code>help</code>" not in listed
    assert "--no-kv-offload" in listed and "nothing: a switch" in listed and "set by the compute service" in listed  # a switch, and a flag that is not the model's to set
    assert '<input type="hidden" name="flag" value="temp">' in page
    assert 'name="value" class="form-select" aria-label="value of reasoning">' in page and '<option value="auto" selected>Auto</option>' in page
    assert '<option value="temp">' not in page and '<option value="host">' not in page  # dedicated and owned flags are not offered as generic flags
    for dedicated in ("temp", "top-p", "top-k", "min-p", "reasoning", "reasoning-effort"):
        assert f"<code>{dedicated}</code>" not in listed and f'<option value="{dedicated}">' not in page
    box = page.split('data-filter="every-flag"')[0].rsplit("<input", 1)[1] + page.split('data-filter="every-flag"')[1].split(">")[0]
    assert 'type="search"' in box and "name=" not in box  # the box that finds a flag is not part of what is saved


def test_the_files_page_lists_every_file_and_a_model_page_shows_what_its_file_says(desk, tmp_path):
    browser, _, _ = desk
    folder = tmp_path / "models"
    folder.mkdir()
    used = gguf(folder / "In-Use.Q4.gguf", general__name="In Use", general__architecture="tiny", tiny__context_length=4096, tiny__block_count=12,
                tokenizer__chat_template="{% if tools %}{% endif %}")  # fmt: skip
    fresh = gguf(folder / "Fresh-Download.Q8.gguf", general__name="Fresh <b>Download</b>", general__architecture="tiny", general__size_label="7B", tiny__context_length=32768)
    Models(str(tmp_path / "loadcoach.db")).put("mine", {"gpus": 1, "flags": {"model": used, "ctx-size": 2048}})

    assert 'href="/files"' not in sign_in(browser, "pat").text and browser.get("/files").status_code == 403
    browser.post("/sign-out")
    sign_in(browser, "jordan")
    assert 'href="/files"' in browser.get("/models?complete=1").text  # Files belongs to the Models category's rail.

    files = browser.get("/files?complete=1").text
    assert 'value="clear-cache"' in files and 'value="consolidate-cache"' in files and 'value="cleanup-nodes"' in files
    assert "Large libraries can take an hour or more" in files and "continues in the background" in files
    refused_cleanup = browser.post("/files", data={"do": "clear-cache"})
    assert "Configure a model storage library before clearing the cache." in refused_cleanup.text
    assert "The form could not be submitted" not in refused_cleanup.text
    assert f"{folder}" in files and "</b> free" in files and 'class="progress-bar"' in files  # the place, and the room on its drive
    # The locations are cards in one collapsible card; the 5-second refresh replaces only its body, so it stays as left.
    assert ">Model file locations<" in files and '<div class="card-body" id="files-places">' in files and 'class="card mb-3 place-card"' in files
    assert "Fresh-Download.Q8.gguf" in files and "In-Use.Q4.gguf" in files and '<a href="/models/mine">mine</a>' in files
    assert "Fresh &lt;b&gt;Download&lt;/b&gt;" in files  # what a file says of itself is text like any other, never HTML
    part = browser.get("/files?part=list").text
    # The pieces alone, each to be put in the place of the one with its id, so that a part of the page someone closed stays closed.
    assert "<html" not in part and part.count('hx-swap-oob="true"') == 7 and "<details" not in part and "Fresh-Download.Q8.gguf" in part
    assert 'id="files-distributions" hx-swap-oob="true"' in part
    assert 'data-refresh-companions="#files-cleanup, #files-distributions, #files-places' in files
    assert 'hx-get="/files?part=list&amp;tier=cache"' in files and files.count("<details") >= 3
    assert '>Cache</a>' in files and '>Storage</a>' in files
    assert "Last used" not in files and "<th data-sort=\"text\">Place</th>" not in files

    # "Add" beside a file no model uses opens the Add form with the file in it and a name made from the file's own.
    link = re.search(r'href="(/models\?file=[^"]+)"', files).group(1).replace("&amp;", "&")
    form = browser.get(link + '&complete=1').text
    assert 'name="artifact" class="form-select"' in form and f'{fresh} · compute · cache' in form and 'value="fresh-download.q8"' in form
    assert "this file was trained for 32,768" in form and re.search(r'<details[^>]*id="add-model"[^>]*open', form)

    # A model's page shows what its file says, and leads to the file's own page, which shows the same.
    its = browser.get("/models/mine").text
    assert "In Use" in its and "trained for 4,096" in its and "its template handles them" in its and "no switch in its template" in its
    assert "<code>tiny.block_count</code>" in its and "Everything it says, 5 entries" in its
    address = "/file?path=" + used
    assert f'href="{address}"' in its
    one = browser.get(address).text
    assert "In-Use.Q4.gguf" in one and "<code>tiny.block_count</code>" in one and '<a href="/models/mine">mine</a>' in one
    assert "Remove that model first" in one and 'value="delete"' not in one  # a file in use offers no way to delete it
    assert browser.get("/file?path=/etc/passwd").status_code == 404


def test_files_separates_cache_and_storage_and_promotes_or_two_click_deletes(tmp_path):
    cache, storage = tmp_path / "cache", tmp_path / "storage"
    for root in (cache, storage):
        for kind in ("llm", "adapters", "projectors"):
            (root / kind).mkdir(parents=True)
    cached = gguf(cache / "llm" / "kept.gguf", general__name="Kept", general__architecture="tiny")
    gguf(storage / "llm" / "kept.gguf", general__name="Kept", general__architecture="tiny")
    stored = gguf(storage / "llm" / "new.gguf", general__name="New", general__architecture="tiny")
    data = tmp_path / "data"
    data.mkdir()
    users = Users(str(data / "accounts.db"), GROUPS)
    for name, group in (("jordan", "admin"), ("panel", "front")):
        users.add(name, group)
        if name != "panel":
            users.change(name, password="correct horse")
    places = {kind: str(cache / kind) for kind in ("llm", "adapters", "projectors")}
    places.update({"storage_" + kind: str(storage / kind) for kind in ("llm", "adapters", "projectors")})
    Models(str(tmp_path / "loadcoach.db")).put("mine", {"gpus": 0, "flags": {"model": cached, "ctx-size": 2048}})
    later = Later()
    switch = Switch(build_loadcoach(Config(database=str(tmp_path / "loadcoach.db"), gpus=(), places=places,
        nvidia_smi="no-such-program", machine_key_file=machine_key(tmp_path), front="http://front:8442"),
        FakeHub({}).client(), door(later, 8442)))
    front = later.app = turnstile_to(tmp_path, users, {8442: switch}, front_settings=True)
    settings = Settings(compute="http://compute", key=users.new_key("panel"), data=str(data))
    with TestClient(build_app(settings, door(front, 8442))) as browser:
        sign_in(browser, "jordan")
        cache_page = browser.get("/files?tier=cache&complete=1").text
        places_html = cache_page.split('id="files-places"', 1)[1].split('<nav class="nav nav-pills', 1)[0].replace("<wbr>", "")
        assert places_html.count(str(cache)) == 1 and places_html.count(str(storage)) == 1
        # A card each for front (nothing set up), the cache and storage; each lists its subfolders, and a drive counts once.
        assert places_html.count('class="card mb-3 place-card"') == 3 and 'aria-label="Storage"' in places_html
        assert places_html.count('<td title="llm">llm</td>') == 2 and places_html.count("</b> free") == 2 and "drives" not in places_html
        assert "Last used" not in cache_page and "Used on nodes" in cache_page
        assert '<a href="/models/mine">mine</a>' in cache_page and "compute" in cache_page

        storage_page = browser.get("/files?tier=storage&complete=1").text
        table = storage_page.split('id="files-storage-table"', 1)[1].split("</table>", 1)[0]
        assert all(heading in table for heading in ("File", "Size", "It says it is", "Buttons"))
        assert "Used by" not in table and "Last used" not in table and "Place" not in table
        assert table.count("Promote to cache") == 1 and "In cache" in table
        assert table.count("data-confirm-two") == 2

        promoted = browser.post("/file", data={"path": stored, "to": "llm", "return": "storage", "do": "copy"})
        assert promoted.url.path == "/files" and promoted.url.params["tier"] == "storage", re.findall(r'<div[^>]*class="[^"]*alert[^>]*>(.*?)</div>', promoted.text, re.S)
        deadline = time.monotonic() + 5
        while not (cache / "llm" / "new.gguf").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (cache / "llm" / "new.gguf").exists() and Path(stored).exists()


def test_a_file_is_moved_and_deleted_from_its_own_page(desk, tmp_path):
    browser, _, _ = desk
    fast, slow = tmp_path / "fast", tmp_path / "slow"
    fast.mkdir(), slow.mkdir()
    used = gguf(fast / "used.gguf", general__name="Used", general__architecture="tiny", tiny__context_length=4096)
    spare = gguf(fast / "spare.gguf", general__name="Spare")
    kept = Models(str(tmp_path / "loadcoach.db"))
    kept.put("mine", {"gpus": 1, "flags": {"model": used, "ctx-size": 2048}})
    # No places are named in this LoadCoach, so the places are the folders its models are in. One model in each keeps both.
    for folder in (fast, slow):
        kept.put(f"stays-{folder.name}", {"gpus": 1, "flags": {"model": gguf(folder / "stays.gguf", general__name="Stays"), "ctx-size": 2048}})
    sign_in(browser, "jordan")

    page = browser.get("/file?path=" + used).text
    assert f'name="to" value="{slow}" title="' in page and f'free there">{slow}</button>' in page and "<details class=\"card" not in page  # one button for the other place, and no cards
    moved = browser.post("/file", data={"path": used, "to": str(slow), "do": "move"})
    assert f"Moving used.gguf to {slow}." in moved.text, re.findall(r'<div[^>]*class="[^"]*alert[^>]*>(.*?)</div>', moved.text, re.S)
    deadline = time.monotonic() + 5
    while (fast / "used.gguf").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (slow / "used.gguf").exists() and kept.stored("mine")["flags"]["model"] == str(slow / "used.gguf")  # its model followed it
    again = browser.post("/file", data={"path": str(slow / "used.gguf"), "to": str(slow), "do": "move"})
    assert again.status_code == 400 and "already has a file named used.gguf" in again.text  # LoadCoach's own words

    # Deleting needs the file's name typed, and LoadCoach is the one that checks it.
    page = browser.get("/file?path=" + spare).text
    assert "type the file" in page and 'value="delete"' in page
    wrong = browser.post("/file", data={"path": spare, "name": "spare", "do": "delete"})
    assert wrong.status_code == 400 and "give its file name exactly: spare.gguf" in wrong.text and Path(spare).exists()
    gone = browser.post("/file", data={"path": spare, "name": "spare.gguf", "do": "delete"})
    assert "Deleted spare.gguf." in gone.text and not Path(spare).exists() and "spare.gguf</a>" not in browser.get("/files?complete=1").text


@pytest.fixture
def live_desk(tmp_path, monkeypatch):
    """weightroom in front of a LoadCoach that really runs, on a port of its own, with a stand-in for llama-server.

    The other fixture reaches LoadCoach without a network, which is quicker but leaves its line
    standing still. Here requests are really answered, so there is something to watch and to stop.
    """
    monkeypatch.setattr("weightroom.app.LOOK_EVERY", 0.05)
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "llama-server"
    fake.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_llama_server.py')} \"$@\"\n")
    fake.chmod(0o755)
    # Two made-up cards of 16000 MiB, and a stand-in for nvidia-smi that answers for them. See fake_nvidia_smi.py.
    cards, smi = tmp_path / "cards", tmp_path / "nvidia-smi"
    cards.mkdir()
    monkeypatch.setenv("FAKE_CARDS", str(cards))
    smi.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).with_name('fake_nvidia_smi.py')} \"$@\"\n")
    smi.chmod(0o755)
    (tmp_path / "small.gguf").touch(), (tmp_path / "tiny.gguf").touch()
    database, port = str(tmp_path / "loadcoach.db"), free_port()
    (tmp_path / "data").mkdir()
    users = Users(str(tmp_path / "data" / "accounts.db"), GROUPS)  # the accounts are the front machine's, beside weightroom's data
    for name, group in (("jordan", "admin"), ("pat", "plain"), ("cam", "plain"), ("panel", "front")):
        users.add(name, group)
        if name != "panel":
            users.change(name, password="correct horse")
    # `mib` is what the stand-in for llama-server then uses of its card.
    models = {name: {"gpus": 1, "flags": {"model": f"{name}.gguf", "ctx-size": 2048, "mib": mib}} for name, mib in (("small", 3000), ("tiny", 1000))}
    # Compute at 127.0.0.2 and turnstile at 127.0.0.1, on the same port, as on two machines.
    key = machine_key(tmp_path)
    loadcoach = build_loadcoach(Config(models=models, gpus=("GPU0", "GPU1"), database=database, llama_server=str(fake), port=port, nvidia_smi=str(smi),
                                       host="127.0.0.2", machine_key_file=key, front=f"http://127.0.0.1:{port}"))
    front = build_turnstile(TurnstileSettings(compute="127.0.0.2", data=str(tmp_path / "data"), machine_key_file=key, ports=(port,)), users=own_copy(users))
    servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning")) for app, host in ((loadcoach, "127.0.0.2"), (front, "127.0.0.1"))]
    running = [threading.Thread(target=server.run) for server in servers]
    for thread in running:
        thread.start()
    while not all(server.started for server in servers):
        time.sleep(0.02)
    settings = Settings(compute="http://127.0.0.2", key=users.new_key("panel"), data=str(tmp_path / "data"), chat_model_url=f"http://127.0.0.1:{port}")
    with TestClient(build_app(settings, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=httpx.Timeout(10, connect=2)))) as browser:
        yield browser, f"http://127.0.0.1:{port}", users
    for server in servers:
        server.should_exit = True
    for thread in running:
        thread.join()


def test_the_first_page_shows_what_compute_is_doing_and_one_request_has_one_page(live_desk):
    browser, compute, users = live_desk
    pat, cam = (httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key(name)}, timeout=30) for name in ("pat", "cam"))
    sign_in(browser, "jordan")
    quiet = browser.get("/status").text
    assert "No requests right now." in quiet and "<b>GPU0</b>" in quiet and "empty" in quiet and "No GPUs are reserved exclusively on the primary compute node." in quiet

    # Pat's request thinks for a long time. Cam's waits behind it.
    answers = []
    looping = threading.Thread(target=lambda: answers.append(pat.post("/v1/chat/completions", json={"model": "small", "drip": 20})))
    looping.start()
    deadline = time.monotonic() + 5
    while "being answered" not in browser.get("/?part=status").text and time.monotonic() < deadline:
        time.sleep(0.05)
    waiting = threading.Thread(target=lambda: answers.append(cam.post("/v1/chat/completions", json={"model": "small"})))
    waiting.start()
    while "waiting" not in browser.get("/?part=status").text and time.monotonic() < deadline:
        time.sleep(0.05)

    status = browser.get("/?part=status").text
    assert "<html" not in status and 'hx-get="/status?part=status"' in status  # the piece that keeps itself true
    assert '<a href="/models/small">small</a>' in status and "being answered" in status and "waiting" in status and ">pat<" in status and ">cam<" in status
    number = re.search(r'<a href="/requests/(\d+)">', status).group(1)

    its = browser.get(f"/requests/{number}").text
    while "round 3." not in its and time.monotonic() < deadline:
        time.sleep(0.05)
        its = browser.get(f"/requests/{number}").text
    assert f"Request {number}" in its and "being answered" in its and "Stop this request" in its
    assert "Thinking" in its and "round 1. round 2. round 3." in its  # what it has been answered so far, read as it passes
    live = browser.get(f"/requests/{number}?part=live").text
    assert "<html" not in live and f'hx-get="/requests/{number}?part=live"' in live

    # Someone who is not pat and has no say sees neither the request nor a way to stop it.
    sign_in(browser, "cam")
    assert browser.get(f"/requests/{number}").status_code == 403 and "1 request of other people" in browser.get("/status").text
    assert "Stopped" not in browser.post(f"/requests/{number}/stop").text
    sign_in(browser, "jordan")

    stopped = browser.post(f"/requests/{number}/stop", data={"back": "home"})
    assert f"Stopped request {number}. The others carry on." in stopped.text
    looping.join(), waiting.join()
    assert [answer.status_code for answer in answers] == [200, 200]  # pat's was cut short, cam's went ahead and was answered
    over = browser.get(f"/requests/{number}").text
    assert "stopped by jordan" in over and "Looked at" in over and "Stop this request" not in over and "hx-get" not in over.split('id="request"')[1].split(">")[0]
    sign_in(browser, "pat")
    assert f'<a href="/requests/{number}">request {number}</a>' in browser.get("/account/logs").text  # pat's own latest entries lead to the same page


def test_the_log_page_shows_what_is_on_record_and_a_request_page_what_was_said(live_desk):
    browser, compute, users = live_desk
    pat = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("pat")}, timeout=30)
    sent = {
        "model": "small", "temperature": 0.2, "tools": [{"type": "function", "function": {"name": "add"}}],
        "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": [{"type": "text", "text": "What is 2+2? <b>"}, {"type": "image_url", "image_url": {}}]}],
        "reply": {"choices": [{"message": {"content": "Four.", "reasoning_content": "Two and two.", "tool_calls": [{"id": "c1", "function": {"name": "add", "arguments": '{"a": 2}'}}]}, "finish_reason": "stop"}]},
    }  # fmt: skip
    number = pat.post("/v1/chat/completions", json=sent).headers["x-loadcoach-request-id"]

    # Its page, once it is over: what it was answered stays, and what was sent can be read.
    sign_in(browser, "pat")
    its = browser.get(f"/requests/{number}").text
    assert "Thinking" in its and "Two and two." in its and "Four." in its and "Asked for the tool <code>add</code>" in its and "&#34;a&#34;: 2" in its  # &#34; is how a page writes a quotation mark
    assert 'class="card-heading">Request</span>' in its and "Be brief." in its and "What is 2+2? &lt;b&gt;" in its and "[image_url, not shown]" in its  # what a caller sent is never run as HTML
    assert "temperature <code>0.2</code>" in its and "1 tool offered: <code>add</code>" in its and "Looked at" not in its

    # The log: one's own entries, each request leading to its one page.
    mine = browser.get("/log?complete=1")
    assert 'href="/log"' in mine.text and f'<a href="/requests/{number}">asked</a>' in mine.text and f'<a href="/requests/{number}">done</a>' in mine.text
    assert 'name="user"' not in mine.text and "<td>jordan</td>" not in mine.text and "status</span> 200" in mine.text

    # Someone who may read everyone's: narrowed by what is typed, which is kept in the address.
    sign_in(browser, "jordan")
    everyone = browser.get("/log?complete=1").text
    assert 'name="user"' in everyone and "<td>pat</td>" in everyone  # jordan only signed in, and sign-ins are on record at the front machine
    loaded = browser.get("/log?kind=model&complete=1").text
    assert ">loaded</a>" in loaded and "<td>request</td>" not in loaded and 'value="model"' in loaded
    part = browser.get("/log?part=list&kind=request&user=pat").text
    assert "<html" not in part and 'hx-get="/log?part=list&amp;kind=request&amp;user=pat&amp;limit=100"' in part and "<td>jordan</td>" not in part
    back = browser.get("/log?before=1&kind=request&complete=1").text
    assert "Nothing on record that fits." in back and "hx-get" not in back.split('id="log"')[1].split(">")[0]  # gone back, it stays as it is

    # Reading what someone else's request said is on record, and shown on its page.
    theirs = browser.get(f"/requests/{number}").text
    assert "Four." in theirs and "Looked at" in browser.get(f"/requests/{number}").text


def test_the_end_of_a_log_is_cut_where_a_line_begins():
    log = "first line\nsecond line\nthird"
    assert tail(log, 100) == log and tail(log, 17) == "second line\nthird" and tail(log, 16) == "third" and tail(log, 5) == "third"
    assert tail(log, 3) == "ird"  # the last line alone is longer: the end of it


def test_a_failure_has_a_page_with_what_was_printed_and_a_model_can_be_asked_about_it(live_desk, tmp_path):
    browser, compute, users = live_desk
    Models(str(tmp_path / "loadcoach.db")).put("broken", {"gpus": 1, "flags": {"model": "small.gguf", "ctx-size": 1, "fail": True}})
    pat = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("pat")}, timeout=30)
    refused = pat.post("/v1/chat/completions", json={"model": "broken", "messages": []})
    assert refused.status_code == 502

    # The request's own page says why it was turned away.
    sign_in(browser, "jordan")
    listed = browser.get("/log?kind=request&what=refused&complete=1").text
    its = browser.get("/requests/" + re.search(r'href="/requests/(\d+)">refused', listed).group(1)).text
    assert "<dt class=\"col-4 col-md-2\">Why</dt>" in its and "cudaMalloc failed: out of memory" in its

    # The model's failure has a page: everything llama-server printed before it gave up, and where to look next.
    number = re.search(r'href="/log/(\d+)">failed</a>', browser.get("/log?kind=model&complete=1").text).group(1)
    entry = browser.get(f"/log/{number}").text
    assert "model failed" in entry and "What llama-server printed last" in entry and "exiting due to model loading error" in entry and "fake: starting with" in entry
    assert "Ask a model about this" in entry and "<option>small</option>" in entry and "<option>broken</option>" in entry and 'hx-post="/ask"' in entry
    assert "/output?at=" not in entry  # this desk has no spotter to read the lines, so no way to them is offered
    # An entry about a request leads to the request's one page. The entry has front's number; the request is known by compute's.
    entry_id, asked_entry = re.search(r'data-value="(\d+)">(?:<time[^>]*>[^<]*</time>|[^<]*)</td>\s*<td>request</td>\s*<td><a href="/requests/(\d+)">asked', browser.get("/log?kind=request&complete=1").text).groups()
    assert str(browser.get(f"/log/{entry_id}").url).endswith(f"/requests/{asked_entry}")
    assert browser.get("/log/99999").status_code == 404

    # Asked about it. The stand-in for a model gives no chat answer, so what is checked is what it was sent, and under whose name.
    answer = browser.post("/ask", data={"log": "0.00.002 E cudaMalloc failed: out of memory", "words": "Why did it not load?", "model": "small"})
    request_number = re.search(r'<a href="/requests/(\d+)">request', answer.text).group(1)
    assert "<html" not in answer.text and "small answered" in answer.text and "It gave no answer." in answer.text
    sent = httpx.get(f"{compute}/loadcoach/requests/{request_number}", headers={"authorization": "Bearer " + users.new_key("jordan")}).json()
    said = sent["text"]["asked"]["messages"]
    assert sent["asked"]["user"] == "jordan" and said[0]["role"] == "system" and "read a log" in said[0]["content"]
    assert said[1]["content"] == "Why did it not load?\n\nThe log:\n0.00.002 E cudaMalloc failed: out of memory"
    # How much of the log is sent can be said: its end, in whole lines.
    assert 'data-log-selection' in entry and 'whole log is sent' in entry and '<table id="printed"' in entry
    short = browser.post("/ask", data={"log": "first line\nsecond line\nthird", "most": "14", "model": "small"})
    kept = httpx.get(f"{compute}/loadcoach/requests/" + re.search(r'/requests/(\d+)', short.text).group(1), headers={"authorization": "Bearer " + users.new_key("jordan")}).json()
    assert kept["text"]["asked"]["messages"][1]["content"].endswith("The log:\nthird")  # fourteen characters reach into "second line", which is left out
    selected = browser.post('/ask', data={'log': 'whole log with unrelated lines', 'selection': 'selected lines only', 'model': 'small'})
    kept = httpx.get(f"{compute}/loadcoach/requests/" + re.search(r'/requests/(\d+)', selected.text).group(1), headers={"authorization": "Bearer " + users.new_key("jordan")}).json()
    assert kept['text']['asked']['messages'][1]['content'].endswith('The log:\nselected lines only')
    nobody = browser.post("/ask", data={"log": "x", "words": "", "model": "no-such-model"})
    assert "does not exist" in nobody.text and "alert-danger" in nobody.text  # refused in LoadCoach's words


def test_the_chat_page_holds_a_conversation_and_shows_an_answer_while_it_is_written(live_desk):
    browser, compute, users = live_desk

    def once_answered(ref):
        """The conversation's page, asked for until no answer is on its way."""
        until = time.monotonic() + 10
        while "data-stream" in (text := browser.get(f"/chat/{ref}").text) and time.monotonic() < until:
            time.sleep(0.05)
        return text

    empty = sign_in(browser, "pat") and browser.get("/chat").text
    assert 'href="/chat"' in empty and "New conversation" in empty and re.search(r'<option value="small"[^>]*>small</option>', empty) and "No conversations yet." in empty
    assert 'name="temporary"' not in empty and versioned_script("chat.html", "chat") in empty  # pat may not send unlogged, so is not offered it

    refused = browser.post("/chat", data={"content": "hello", "model": "no-such-model"})
    assert refused.status_code == 400 and "does not exist" in refused.text and ">hello</textarea>" in refused.text  # LoadCoach's words, and what was typed kept
    started = browser.post("/chat", data={"content": "Make **tea** <b>now</b> and think", "model": "small", "thinking": "on", "system": "Be brief."})
    ref = str(started.url).rsplit("/", 1)[1]
    its = once_answered(ref)
    # The person's words as they typed them. The answer's Markdown made into HTML, and HTML a model wrote shown, never run.
    assert '<div class="said-user">Make **tea** &lt;b&gt;now&lt;/b&gt; and think</div>' in its
    assert "<p>You said: Make <strong>tea</strong> &lt;b&gt;now&lt;/b&gt; and think</p>" in its and "<b>now</b>" not in its
    assert "Thought first" in its and "Let me think. Done thinking." in its and "<details class=\"thinking\" >" in its  # folded away once the answer is there
    assert '<div class="kept"><pre>Let me think. Done thinking.</pre>' in its  # and opened, it is no taller than the others and scrolls
    assert "tokens in" in its and re.search(r'<a href="/requests/\d+">request \d+</a>', its) and 'data-copy="answer"' in its
    assert f'<a class="chat-link active" href="/chat/{ref}"' in its and 'value="again">Ask again</button>' in its and ">Edit</summary>" in its
    assert re.search(r'<option value="small"[^>]* selected>small</option>', its) and ">Be brief.</textarea>" in its

    # While an answer is written the page names where to follow it, and that gives each step as HTML ready to show.
    browser.post(f"/chat/{ref}", data={"do": "say", "content": "answer slowly one *two* three", "model": "small"})
    waiting = browser.get(f"/chat/{ref}").text
    assert f'data-stream="/chat/{ref}/stream"' in waiting and 'value="stop" data-confirm-two>Stop</button>' in waiting and 'value="say">Send</button>' not in waiting
    steps = browser.get(f"/chat/{ref}/stream").text
    assert steps.count('data: <div class="answer">') > 2 and "<em>two</em>" in steps and steps.rstrip().endswith("event: done\ndata:")
    middle = browser.get(f"/chat/{ref}?part=main").text
    assert "<html" not in middle and "<em>two</em> three" in middle and 'value="say">Send</button>' in middle  # the middle of the page alone, once it is over

    # Thinking, while it is written, is shown whole and open, in a box with nothing to scroll inside.
    browser.post(f"/chat/{ref}", data={"do": "say", "content": "think slowly about it", "model": "small"})
    thinking = browser.get(f"/chat/{ref}/stream").text
    assert '<details class="thinking" open>' in thinking and '<div class="growing"><pre>Let me think. Done thinking.</pre>' in thinking
    once_answered(ref)

    # Stopped part-way: what came is kept, and more of it can be asked for.
    browser.post(f"/chat/{ref}", data={"do": "say", "content": "count slowly one two three four five six seven eight nine ten", "model": "small"})
    time.sleep(0.7)
    browser.post(f"/chat/{ref}", data={"do": "stop"})
    cut = once_answered(ref)
    assert "This answer ended before it was finished." in cut and 'value="continue">Continue</button>' in cut
    browser.post(f"/chat/{ref}", data={"do": "continue", "model": "small"})
    assert "and so on." in once_answered(ref)
    browser.post(f"/chat/{ref}", data={"do": "edit", "content": "something else", "model": "small"})
    edited = once_answered(ref)
    assert "You said: something else" in edited and "count slowly" not in edited

    # How it is to be answered is under the message box all through the conversation: the instructions, shown in short, and four numbers.
    assert 'data-composer-options' in edited and "System prompt</label>" in edited and all(f'name="{name}"' in edited for name in ("temperature", "top_k", "top_p", "min_p"))
    wrong = browser.post(f"/chat/{ref}", data={"do": "say", "content": "hello", "model": "small", "tuning": "1", "system": "Be kind.", "temperature": "hot"})
    assert wrong.status_code == 400 and "temperature is a number from 0 to 2." in wrong.text and 'value="hot"' in wrong.text and ">Be kind.</textarea>" in wrong.text
    browser.post(f"/chat/{ref}", data={"do": "say", "content": "hello again", "model": "small", "tuning": "1", "system": "Be kind.", "temperature": "0.2", "top_k": "20", "thinking": "on"})
    tuned = once_answered(ref)
    assert 'name="temperature" type="number" min="0" max="2" step="any" class="form-control form-control-sm" inputmode="decimal" value="0.2"' in tuned and ">Be kind.</textarea>" in tuned and 'name="top_p" type="number" min="0" max="1" step="any" class="form-control form-control-sm" inputmode="decimal" value=""' in tuned
    # It was sent with that very message: the model was told so, and given the numbers.
    number = re.findall(r'<a href="/requests/(\d+)">request', tuned)[-1]
    sent = httpx.get(f"{compute}/loadcoach/requests/{number}", headers={"authorization": "Bearer " + users.new_key("pat")}).json()["text"]["asked"]
    assert sent["messages"][0] == {"role": "system", "content": "Be kind."} and (sent["temperature"], sent["top_k"]) == (0.2, 20) and "top_p" not in sent

    # Its name is changed from its page, which leaves the rest as it is. Nobody else can open it. Deleted, it is gone.
    renamed = browser.post(f"/chat/{ref}", data={"do": "rename", "title": "Tea & cake"}).text
    assert "Tea &amp; cake" in renamed and ">Be kind.</textarea>" in renamed
    sign_in(browser, "cam")
    assert browser.get(f"/chat/{ref}").status_code == 404 and "Tea" not in browser.get("/chat").text
    sign_in(browser, "pat")
    gone = browser.post(f"/chat/{ref}", data={"do": "delete"})
    assert str(gone.url).endswith("/chat") and "Tea" not in gone.text and browser.get(f"/chat/{ref}").status_code == 404


def test_model_file_locations_are_selected_and_saved_per_machine(live_desk, tmp_path):
    browser, _, _ = live_desk
    sign_in(browser, "jordan")
    page = browser.get("/settings").text
    assert 'id="model-files"' in page and 'name="file_machine"' in page
    assert 'name="models_folder"' in page and 'name="models_storage_folder"' in page
    locations = browser.post("/settings/model-files", data={"file_machine": "compute", "models_folder": str(tmp_path / "cache"),
                                                              "models_storage_folder": str(tmp_path / "storage")})
    assert "Saved. Restart LoadCoach on that machine" in locations.text


def test_the_settings_page_shows_every_program_and_changes_what_may_be_changed(live_desk, tmp_path):
    browser, _, _ = live_desk
    assert 'href="/settings"' not in sign_in(browser, "pat").text and browser.get("/settings").status_code == 403
    sign_in(browser, 'jordan')
    assert 'href="/settings"' in browser.get('/settings').text
    until = time.monotonic() + 5
    while 'action="/settings/weightroom"' not in (page := browser.get("/settings").text) and time.monotonic() < until:  # weightroom says what it runs with at its next look at compute
        time.sleep(0.05)
    assert ">loadcoach</summary>" not in page and ">spotter</summary>" not in page and 'action="/settings/weightroom"' in page and "Display and costs" in page
    # Node-owned programs and hardware policy have a dedicated non-collapsible page.
    compute_page = browser.get('/compute-nodes').text
    assert compute_page.index('id="machine-key"') < compute_page.index('id="manage-gpus"') < compute_page.index('>loadcoach</h2>')
    assert 'name="value-keep_text" class="form-control font-monospace" value="50"' in compute_page
    assert 'name="value-disabled_gpus"' not in compute_page and 'name="value-gpus"' not in compute_page
    assert 'name="value-disable_ram_offload"' in compute_page and 'name="value-host_ram_headroom_mib"' in compute_page and 'name="value-host_ram_limit_mib"' in compute_page
    assert 'name="value-llama_server"' not in compute_page and "Read-only machine values" in compute_page
    assert 'aria-label="Read-only machine values"' in compute_page and "port" in compute_page
    assert 'name="value-mock_input_rate" class="form-control font-monospace" value="2.0"' in page
    assert 'name="value-mock_output_rate" class="form-control font-monospace" value="10.0"' in page
    assert "Third-party services to track" in page
    for name in ("ollama", "docker"):
        assert "checked" in page.split(f'id="third-party-{name}"', 1)[1].split(">", 1)[0]
    for name in ("apache", "nginx"):
        assert "checked" not in page.split(f'id="third-party-{name}"', 1)[1].split(">", 1)[0]

    saved = browser.post("/settings/loadcoach", data={"machine": "compute", "value-keep_text": "7", "value-unlogged": "true", "value-gpus": '["A"]', "value-max_wait": "600"})
    assert "Saved." in saved.text and saved.text.count("Changed here by jordan") == 2 and "It is in force." not in saved.text  # what did not differ was not sent, nor the cards, which are the machine's
    assert 'value="7"' in saved.text and '>loadcoach</h2>' in saved.text
    wrong = browser.post("/settings/loadcoach", data={"machine": "compute", "value-keep_text": '"many"'})
    assert wrong.status_code == 400 and "keep_text: It is a number, and this is text." in wrong.text and 'value="&quot;many&quot;"' in wrong.text  # refused in LoadCoach's words, what was typed kept
    assert "not written as a settings file would hold it" in browser.post("/settings/loadcoach", data={"machine": "compute", "value-keep_text": "many"}).text
    back = browser.post("/settings/loadcoach", data={"machine": "compute", "reset": "keep_text"})
    assert 'name="value-keep_text" class="form-control font-monospace" value="50"' in back.text and back.text.count("Changed here by jordan") == 1

    assert "From the file." not in page and "A change holds at once." not in page
    assert "The file says" not in saved.text and "Back to the file" not in saved.text

    # A program on the front machine: its restart requirement is highlighted.
    front = browser.post("/settings/weightroom", data={"value-timezone": '"Europe/Paris"'})
    assert 'Needs restart · weightroom' in front.text
    copy = tmp_path / "data" / "settings.json"
    while (not copy.exists() or "Paris" not in copy.read_text()) and time.monotonic() < until + 5:
        time.sleep(0.05)
    assert json.loads(copy.read_text())["weightroom"] == {"timezone": "Europe/Paris"}
    assert "Nothing was changed." in browser.post("/settings/weightroom", data={"value-timezone": '"Europe/Paris"'}).text
    services = browser.post("/settings/weightroom", data={"third-party-services-present": "1", "third-party-apache": "on"})
    assert "Saved." in services.text
    while (not copy.exists() or json.loads(copy.read_text()).get("weightroom", {}).get("third_party_services") != ["apache"]) and time.monotonic() < until + 5:
        time.sleep(0.05)
    assert json.loads(copy.read_text())["weightroom"]["third_party_services"] == ["apache"]


def test_the_controls_do_what_they_say_and_only_for_those_who_may(live_desk):
    browser, compute, users = live_desk
    pat = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("pat")}, timeout=30)
    assert "Controls" not in sign_in(browser, "pat").text  # nothing pat may press, so nothing is offered
    assert "may not" in browser.post("/control", data={"do": "clear"}).text  # and LoadCoach refuses if pat tries anyway
    sign_in(browser, 'jordan')
    assert 'data-confirm-two' in browser.get('/status').text

    assert pat.post("/v1/chat/completions", json={"model": "small"}).status_code == 200
    assert "<a href=\"/models/small\">small</a>" in browser.get("/?part=status").text
    # The Lock table: the open lock locks the model in place, and Unload all then says why it will not.
    assert 'value="lock-model"' in browser.get("/?part=status").text
    assert "Locked small in place." in browser.post("/control", data={"do": "lock-model", "model": "small"}).text
    assert 'value="unlock-model"' in browser.get("/?part=status").text
    refused = browser.post("/control", data={"do": "unload"})
    assert "Locked in place: small." in refused.text and "alert-danger" in refused.text
    assert "Unlocked small." in browser.post("/control", data={"do": "unlock-model", "model": "small"}).text
    asked = browser.post("/control", data={"do": "unload"})
    assert "Asked for every model to be unloaded." in asked.text
    deadline = time.monotonic() + 5
    while "No requests right now." not in browser.get("/?part=status").text and time.monotonic() < deadline:  # the unloading is itself in line until it is done
        time.sleep(0.05)
    assert "empty" in browser.get("/?part=status").text and "/models/small" not in browser.get("/?part=status").text

    assert "Turned away 0 waiting requests." in browser.post("/control", data={"do": "clear"}).text
    assert pat.post("/loadcoach/lock", json={"seconds": 60}).status_code == 403  # pat may not lock; jordan's key can
    jordan = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("jordan")}, timeout=30)
    assert jordan.post("/loadcoach/lock", json={"seconds": 60}).status_code == 200
    assert "held by <b>jordan</b>" in browser.get("/?part=status").text
    assert "The lock is broken." in browser.post("/control", data={"do": "unlock"}).text and "No GPUs are reserved exclusively on the primary compute node." in browser.get("/?part=status").text

    # Taking no new requests: said on the first page to everyone, ended there by those who may.
    assert "No new requests are taken. Nothing was under way." in browser.post("/control", data={"do": "drain", "why": "new drives."}).text
    status = browser.get("/?part=status").text
    assert "asked by jordan: new drives." in status and "0 still here." in status and "Take requests again" in status
    turned_away = pat.post("/v1/chat/completions", json={"model": "small"})
    assert turned_away.status_code == 503 and "new drives." in turned_away.json()["error"]["message"]
    sign_in(browser, "pat")
    assert "asked by jordan: new drives." in browser.get("/?part=status").text and "Take requests again" not in browser.get("/?part=status").text
    assert "may not" in browser.post("/control", data={"do": "open"}).text
    sign_in(browser, "jordan")
    assert "Requests are taken again." in browser.post("/control", data={"do": "open"}).text and "No new requests are taken" not in browser.get("/?part=status").text
    assert pat.post("/v1/chat/completions", json={"model": "small"}).status_code == 200


@pytest.fixture
def desk_with_machine(tmp_path, monkeypatch):
    """weightroom in front of both LoadCoach and spotter, each behind a switch of its own.

    The programs spotter runs are stood in for by small scripts, as in spotter's own checks.
    """
    monkeypatch.setattr("weightroom.app.LOOK_EVERY", 0.05)
    stand_ins, asked = tmp_path / "bin", tmp_path / "asked.txt"
    stand_ins.mkdir()
    for name, script in (
        ("nvidia-smi", 'echo "0, RTX Made Up, 51, 36, 77.31, 45, 15756, 16311, 180.00"\necho "1, RTX Made Up, 88, [N/A], 86.14, [N/A], 1024, 16311, [N/A]"\n'),
        ("systemctl", f'echo "$@" >> {asked}\ncase "$2" in show) printf "ActiveState=active\\nSubState=running\\nNRestarts=2\\nMemoryCurrent=1048576\\n";; esac\n'),
        ("journalctl", 'echo "2026-10-08T10:00:00 loadcoach <b>started</b>"\n'),
        ("gdbus", 'echo "(uint64 120000,)"\n'),  # someone is signed in at the machine's own screen, and last touched it two minutes ago
        ("loginctl", 'printf "1000 jordan\n1001 pat\n1000 jordan\n"\n'),
    ):
        (stand_ins / name).write_text("#!/bin/sh\n" + script)
        (stand_ins / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_ins}:/usr/bin:/bin")

    database = str(tmp_path / "loadcoach.db")
    (tmp_path / "data").mkdir()
    users = Users(str(tmp_path / "data" / "accounts.db"), GROUPS)  # the accounts are the front machine's, beside weightroom's data
    for name, group in (("jordan", "admin"), ("pat", "plain"), ("panel", "front")):
        users.add(name, group)
        if name != "panel":
            users.change(name, password="correct horse")
    key = machine_key(tmp_path)
    compute = Switch(build_loadcoach(Config(database=database, gpus=("A", "B"), nvidia_smi="no-such-program", machine_key_file=key)))
    watcher_settings = spotter.Settings(services={"loadcoach": "loadcoach.service"},
        system_services={"ollama": "ollama.service", "docker": "docker.service", "apache": "apache2.service", "nginx": "nginx.service"},
        machine_key_file=key, database=database, history=str(tmp_path / "spotter.db"), disks=(str(tmp_path),))
    machine = Switch(spotter.build_app(watcher_settings))
    front = turnstile_to(tmp_path, users, {8442: compute, 8444: machine})
    settings = Settings(compute="http://compute", key=users.new_key("panel"), data=str(tmp_path / "data"), spotter="http://spotter")
    # Match the authenticated internal image/audio loopback initialized by the real
    # Turnstile lifespan, without starting its compute polling or background jobs.
    from loadcoach import observability as observation
    internal = door(observation.internal(front), 8442)
    front.state.mirrorwall.batches.door = internal
    with TestClient(build_app(settings, door(front, 8442), door(front, 8444))) as browser:
        try:
            yield browser, compute, machine, users, asked
        finally:
            browser.portal.call(internal.aclose)


def test_the_strip_of_meters_is_on_every_page_until_someone_chooses_not_to_see_it(desk_with_machine):
    browser, _, machine, users, _ = desk_with_machine
    home = sign_in(browser, "pat").text
    assert 'id="strip"' in home and 'data-telemetry-stream="/strip/stream"' in home and 'href="/status"' in home
    assert 'hx-get="/strip"' not in home and versioned_script("frame.html", "pieces") in home
    assert home.index('</nav>') < home.index('id="strip"') < home.index('</header>')
    assert 'href="/telemetry"' in browser.get('/status').text

    strip = browser.get("/strip").text
    assert "<html" not in strip and "RAM" in strip and "CPU" in strip
    assert "temperature" not in strip
    overview = browser.get("/telemetry").text
    assert "No telemetry is available" in overview  # this fixture does not start turnstile's background poll
    assert "temperature" not in overview

    # Whether to see it is each person's own choice, kept with them on compute.
    account = browser.get("/account/appearance").text
    assert 'id="strip"' in account
    assert 'name="show_telemetry" type="checkbox" class="form-check-input" checked' in account
    kept = browser.post("/account/display", data={})  # the box unticked
    assert "Kept." in kept.text and 'id="strip"' not in kept.text
    assert users.choices("pat") == {"show_telemetry": False}
    home = browser.get("/").text
    assert 'id="strip"' not in home
    appearance = browser.get("/account/appearance").text
    assert "checked" not in appearance.split('name="show_telemetry"')[1].split(">")[0]
    restored = browser.post("/account/display", data={"show_telemetry": "on"}).text
    assert 'id="strip"' in restored

    machine.off = True
    assert "RAM" in browser.get("/strip").text


def test_a_form_that_changes_a_piece_of_its_page_names_a_piece_that_is_there(desk_with_machine):
    browser, _, _, users, _ = desk_with_machine
    sign_in(browser, "jordan")
    # htmx takes the piece a form names out of the page that comes back. A name that is on no page would leave nothing in its place.
    pieces = {"/log?complete=1": "log-view", "/machine": "stretch", "/account/logs": "usage", "/output?source=loadcoach": "page-content", "/fingerprints": "found", "/hub": "hub-found",
              "/benchmarks/runs": "benchmark-choice", "/benchmarks/compare": "compare-content", "/cloud-apis": "page-content", "/roles": "page-content", "/settings": "page-content",
              "/users/pat": "page-content", "/groups/plain": "page-content"}  # fmt: skip
    for address, piece in pieces.items():
        page = browser.get(address)
        named = set(re.findall(r'hx-select="#([^"]+)"', page.text))
        assert page.status_code == 200 and piece in named and all(f'id="{name}"' in page.text for name in named), address
    # A form that changes something leaves the address bar alone. One that shows something puts what was asked for in it.
    assert 'hx-push-url="false"' in browser.get("/users/pat").text and 'hx-push-url="false"' not in browser.get("/log?complete=1").text
    # Removing an account leads to another page, so that form is sent the usual way.
    assert "hx-" not in browser.get("/users/pat").text.split('value="remove"')[0].rsplit("<form", 1)[1]
    assert browser.get("/static/pieces.js").status_code == 200 and versioned_script("frame.html", "pieces") in browser.get("/log?complete=1").text


def test_every_button_result_uses_the_shared_temporary_notice():
    pages = Path(__file__).parents[1] / "weightroom" / "pages"
    for template in pages.glob("*.html"):
        if template.name == "parts.html":
            continue
        text = template.read_text()
        assert "{% if said %}" not in text, template.name
    script = (Path(__file__).parents[1] / "weightroom" / "static" / "pieces.js").read_text()
    style = (Path(__file__).parents[1] / "weightroom" / "static" / "style.css").read_text()
    assert 'setTimeout(() => notice.remove(), 250)' in script and '}, 7000);' in script
    assert '.said-box { position: fixed; top: calc(var(--top, 6rem) + .75rem)' in style


def test_benchmark_runs_offers_one_model_outstanding_queue(desk):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    page = browser.get("/benchmarks/runs")
    assert page.status_code == 200
    assert 'id="outstanding-model"' in page.text
    assert "Queue outstanding benchmarks" in page.text
    assert "model settings, benchmark protocol, inference engine or machine changes" in page.text


def test_outstanding_partial_result_shows_queued_and_each_unavailable_plan(desk):
    browser, _, _ = desk
    sign_in(browser, 'jordan')
    route = next(one for one in browser.app.state.benchmarks.routes if one.path == '/benchmarks/outstanding')

    async def partial_result(scope, receive, send):
        response = JSONResponse({'queued': 1, 'runs': ['batch_queued'], 'fresh': ['speed at compute'],
            'active': ['load at compute'], 'unavailable': ['context at rig'],
            'errors': [{'benchmark': 'tool_use at compute', 'status': 400,
                        'message': 'This location does not support tool calls.'}]}, status_code=207)
        await response(scope, receive, send)

    route.app = partial_result
    page = browser.post('/benchmarks/runs', data={'action': 'outstanding', 'model': 'small'}, follow_redirects=True)
    assert page.status_code == 200
    assert 'Queued 1 outstanding benchmark.' in page.text
    assert '1 already current.' in page.text and '1 already queued or running.' in page.text
    assert 'Environment unavailable: context at rig.' in page.text
    assert 'Could not queue: tool_use at compute (This location does not support tool calls.).' in page.text
    assert 'class="alert alert-warning said said-box" role="alert"' in page.text


def test_the_machine_page_shows_telemetry_over_time_and_its_services(desk_with_machine, tmp_path):
    browser, _, _, _, asked = desk_with_machine
    readings = sqlite3.connect(tmp_path / "spotter.db")
    now = time.time()
    for ago, heat in ((3000, 40), (2940, 45), (2880, 50), (600, 65), (540, 70)):  # nothing was read for a long while in the middle
        kept = {"free on /": 2**30, "memory used": 2**30, "processor load": 5, "GPU 1 load": 9, "GPU 0 temperature": heat, "GPU 0 memory used": 8 * 2**30}
        readings.execute("INSERT INTO readings VALUES (?, ?)", (now - ago, json.dumps(kept)))
    readings.commit()

    page = sign_in(browser, "pat")
    page = browser.get("/machine?hours=1").text
    assert "GPU 0 temperature" in page and "<b>70°</b>" in page and "40° to 70°" in page and "8.0 GB" in page
    # Each GPU under a heading of its own, then the processor and the memory, then the drives.
    assert [page.index(f">{heading}</h2>") for heading in ("GPU 0", "GPU 1", "Processor and memory", "Storage")] == sorted(page.index(f">{heading}</h2>") for heading in ("GPU 0", "GPU 1", "Processor and memory", "Storage"))
    assert page.index(">Memory used<") < page.index(">Temperature<") < page.index(">Processor load<") < page.rindex(">Memory used<") < page.index(">Free on /<")
    heat = next(figure for figure in page.split("<figure") if "GPU 0 temperature" in figure)
    told = json.loads(heat.split("data-chart='")[1].split("'")[0])  # what the browser draws the line from
    assert (told["unit"], told["heat"], len(told["values"])) == ("°", [45, 85], 60) and len(json.loads(page.split("data-times='")[1].split("'")[0])) == 60  # an hour, a minute to a step
    gaps = "".join("-" if value is None else "x" for value in told["values"]).strip("-")
    assert "x-" in gaps and "-x" in gaps and 70 in told["values"]  # where nothing was read there is nothing, not a nought
    chart_script = browser.get("/static/charts.js").text
    assert 'src="/static/echarts.min.js"' in page and 'src="/static/charts.js?v=4"' in page and "echarts.min.js" not in browser.get("/status").text
    assert "connectNulls: true" in chart_script and 'areaStyle: { opacity: 0.18, origin: "start" }' in chart_script and 'type: "slider"' in chart_script and 'type: "inside"' in chart_script
    assert "dataZoom" in browser.get("/static/usage.js").text and "dataZoom" in browser.get("/static/reliability.js").text
    assert "loadcoach.service" in page and "active" in page and "Restart loadcoach" not in page and "What it printed" not in page  # pat may look, not act
    assert browser.post("/machine/services/loadcoach/restart").status_code == 400 and "may not" in browser.post("/machine/services/loadcoach/restart").text
    assert "restart" not in (asked.read_text() if asked.exists() else "").replace("NRestarts", "")

    sign_in(browser, "jordan")
    page = browser.get("/machine").text
    assert "Restart loadcoach" in page and "What it printed" in page
    assert "ollama.service" in page and "docker.service" in page and "apache2.service" not in page and "nginx.service" not in page
    done = browser.post("/machine/services/loadcoach/restart")
    assert "Asked loadcoach to restart. It is now active." in done.text and "--user restart loadcoach.service" in asked.read_text().splitlines()
    # What a service printed has a page of its own, reached from here: the latest lines, or those around a moment.
    assert 'href="/output?source=loadcoach"' in page and 'href="/output?source=kernel"' in page
    printed = browser.get("/output?source=loadcoach").text
    assert re.search(r'<td><time[^>]+data-local-time[^>]*>[^<]+</time> loadcoach &lt;b&gt;started&lt;/b&gt;</td>', printed)  # what a service printed is text, never HTML
    assert "<option selected>loadcoach</option>" in printed and "<option >kernel</option>" in printed and 'data-filter="printed"' in printed
    assert "<option >ollama</option>" in printed and "<option >docker</option>" in printed and ">apache</option>" not in printed and ">nginx</option>" not in printed
    hidden = browser.get("/output?source=apache")
    assert hidden.status_code == 400 and "apache is not selected under WeightRoom settings." in hidden.text
    deadline = time.monotonic() + 5
    while 'id="third-party-apache"' not in browser.get("/settings?open=weightroom").text and time.monotonic() < deadline:
        time.sleep(0.05)
    changed = browser.post("/settings/weightroom", data={"third-party-services-present": "1", "third-party-apache": "on"})
    assert "Saved." in changed.text
    deadline = time.monotonic() + 5
    while "apache2.service" not in (changed_machine := browser.get("/machine").text) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "apache2.service" in changed_machine and "ollama.service" not in changed_machine and "docker.service" not in changed_machine and "nginx.service" not in changed_machine
    changed_logs = browser.get("/output").text
    assert "<option >apache</option>" in changed_logs and ">ollama</option>" not in changed_logs and ">docker</option>" not in changed_logs and ">nginx</option>" not in changed_logs
    blocked = browser.post("/machine/services/docker/restart")
    assert blocked.status_code == 400 and "docker is not selected under WeightRoom settings." in blocked.text
    around = browser.get("/output?source=kernel&at=2026-10-08T12:00:00&before=300&after=60")
    assert around.status_code == 200 and "<option selected>kernel</option>" in around.text and 'value="2026-10-08T12:00:00"' in around.text and 'value="300" aria-label="Seconds before"' in around.text
    assert browser.get("/output?at=noon").status_code == 400 and "spotter knows the services" in browser.get("/output?source=nothing").text
    sign_in(browser, "pat")
    assert "restart-services ability" in browser.get("/output").text and browser.get("/output").status_code == 403  # reading what was printed needs the same ability as acting
    sign_in(browser, "jordan")

    # Power: the logged-in count is informational, and those who may act need no separate acknowledgement for it.
    assert "Compute never goes to sleep by itself." in page and ">Sleep</button>" in page
    assert "2 users are logged in to compute." in page and 'name="desktop"' not in page
    going = browser.post("/machine/power/sleep", data={"force": "on"})
    assert "Compute will go to sleep now, whatever LoadCoach is doing." in going.text
    deadline = time.monotonic() + 3
    while "suspend" not in asked.read_text() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "--no-ask-password suspend --check-inhibitors=no" in asked.read_text().splitlines()
    assert 'id="power-now"' in browser.get("/machine?part=power").text and "<html" not in browser.get("/machine?part=power").text
    sign_in(browser, "pat")
    assert ">Sleep</button>" not in browser.get("/machine").text and "2 users are logged in to compute." in browser.get("/machine").text
    assert "may not" in browser.post("/machine/power/off").text
    assert "poweroff" not in asked.read_text()

    drawn = chart("GPU 0 temperature", [40, None, 50, 70])
    assert drawn["told"] == {"values": [40, None, 50, 70], "unit": "°", "top": None, "heat": [45, 85]} and (drawn["low"], drawn["high"], drawn["last"]) == ("40°", "70°", "70°")
    assert chart("GPU 0 load", [None, None])["told"] is None and chart("processor temperature", [50])["told"]["heat"] == [45, 90]
    assert chart("memory used", [2**30], top=2**31)["told"] == {"values": [1.0], "unit": " GB", "top": 2.0, "heat": [0, 2.0]}  # a size is drawn in GB
    assert chart("free on /", [2**30], top=2**32)["told"]["heat"] == [4.0, 0]  # room on a drive is the other way up: red when little is left
    assert chart("GPU 1 power", [50])["told"]["heat"] is None and chart("GPU 1 power", [50], top=180)["told"]["heat"] == [0, 180]  # no known most, no colour scale


def test_with_loadcoach_down_and_the_machine_up_someone_can_still_sign_in_and_start_it_again(desk_with_machine):
    browser, compute, machine, users, asked = desk_with_machine
    sign_in(browser, "jordan")
    compute.off = True

    # A sign-in from before still counts: the accounts are on this machine, and turnstile says whose key it is.
    off = browser.get("/")
    assert off.status_code == 200 and "Node unavailable: compute" in off.text
    assert "Restart loadcoach" in browser.get("/machine").text

    # A fresh sign-in is checked by turnstile, here, whatever compute is doing.
    browser.post("/sign-out")
    assert "Wrong name or password." in sign_in(browser, "jordan", "wrong horse").text
    sign_in(browser, "jordan")
    assert "Restart loadcoach" in browser.get("/machine").text
    assert users.by_key(browser.cookies["weightroom"]).name == "jordan"  # a real session key, good at LoadCoach too once it is back
    assert "Asked loadcoach to start." in browser.post("/machine/services/loadcoach/start").text and "--user start loadcoach.service" in asked.read_text()

    compute.off = False
    assert '<h1 class="page-title wrap">Status</h1>' in browser.get("/").text  # LoadCoach is back, and the same sign-in carries on

    # With both down, anyone still signs in, and sees what compute last did.
    compute.off = machine.off = True
    browser.post("/sign-out")
    assert "Node unavailable: compute" in sign_in(browser, "pat").text


def test_voice_settings_edit_single_profile_fields_and_named_speaker(desk, tmp_path):
    from copy import deepcopy
    from loadcoach.settings import Changes, said

    browser, _, users = desk
    models = Models(str(tmp_path / "loadcoach.db"))
    for name in ("small", "alternate", "not-allowed"):
        models.put(name, {"gpus": 0, "flags": {"model": name + ".gguf"}})
    users.change("jordan", own={"models": ["small", "alternate"]})
    store = Changes(str(tmp_path / "loadcoach.db"))
    profile = dict(model="small", modes=["direct", "hermes"], system_prompt="Speak briefly.",
                   max_output_tokens=128, max_context_tokens=8192, speaker=3)
    original = dict(profiles={"default": profile, "spare": {"model": "keep-me"}}, idle_seconds=120,
                    synthesis=dict(model="/models/kokoro-multi-lang-v1_0/model.onnx",
                                   voices="/models/kokoro-multi-lang-v1_0/voices.bin"))
    store.seen("voice", said(original, original, {}))
    sign_in(browser, "jordan")
    page = browser.get("/system/voice?open=voice").text
    assert 'name="value-profiles"' not in page
    assert 'name="value-profiles.default.model"' in page
    assert 'name="value-profiles.default.model" class="form-select"' in page
    assert '<option value="small" selected>small</option>' in page
    assert '<option value="alternate">alternate</option>' in page
    assert '<option value="not-allowed"' not in page
    assert 'name="value-profiles.default.system_prompt"' in page
    assert 'name="value-profiles.default.speaker" class="form-select"' in page
    assert '<option value="3" selected>3 — af_heart</option>' in page
    assert '<option value="53">53 — em_santa</option>' in page
    assert 'name="value-profiles.default.modes"' in page

    saved = browser.post("/settings/voice", data={"value-profiles.default.speaker": "20",
        "value-profiles.default.model": "alternate",
        "value-profiles.default.system_prompt": 'Say "hello".\nKeep it short.',
        "value-profiles.default.max_output_tokens": "64", "value-idle_seconds": "60"})
    assert saved.status_code == 200 and "Saved." in saved.text
    changed = store.of("voice")
    assert changed["idle_seconds"] == 60
    assert changed["profiles"]["default"] == {**profile, "speaker": 20, "model": "alternate",
        "system_prompt": 'Say "hello".\nKeep it short.', "max_output_tokens": 64}
    assert changed["profiles"]["spare"] == original["profiles"]["spare"]
    assert '<option value="20" selected>20 — bf_alice</option>' in saved.text
    assert 'value="profiles.default.speaker"' in saved.text  # individual reset
    assert 'value="profiles.default.modes"' not in saved.text  # unchanged field has no reset
    assert "voice has not taken it up yet." not in saved.text

    # Refused input leaves every change intact, including another valid field in this form.
    for field, value in (("speaker", "999"), ("speaker", "true"), ("max_output_tokens", "0"),
                         ("modes", '["unknown"]'), ("model", ""), ("model", "not-allowed"), ("model", "missing")):
        refused = browser.post("/settings/voice", data={"value-profiles.default." + field: value,
                                                       "value-idle_seconds": "10"})
        assert refused.status_code == 400
        assert store.of("voice") == changed

    # Reset one field after it took effect, before the service reports the reset.
    running = deepcopy(original)
    running.update(changed)
    store.seen("voice", said(running, original, {}))
    reset = browser.post("/settings/voice", data={"reset": "profiles.default.speaker"})
    assert reset.status_code == 200
    assert '<option value="3" selected>3 — af_heart</option>' in reset.text
    assert "voice has not taken it up yet." not in reset.text
    assert store.of("voice")["profiles"]["default"]["speaker"] == 3
    assert store.of("voice")["profiles"]["default"]["max_output_tokens"] == 64
    running = deepcopy(original)
    running.update(store.of("voice"))
    store.seen("voice", said(running, original, {}))
    assert "It is in force." not in browser.get("/system/voice?open=voice").text
    for name in ("system_prompt", "max_output_tokens", "model"):
        assert browser.post("/settings/voice", data={"reset": "profiles.default." + name}).status_code == 200
    assert store.of("voice") == {"idle_seconds": 60}
    sign_in(browser, "pat")
    assert browser.post("/settings/voice", data={"value-profiles.default.speaker": "2"}).status_code == 403
    assert store.of("voice") == {"idle_seconds": 60}


def test_voice_speaker_names_are_not_reused_for_a_different_bundle():
    from weightroom.voice_settings import speaker_choices
    settings = [dict(name="synthesis", value=dict(model="/models/kokoro-int8-multi-lang-v1_1/model.int8.onnx",
                                                 voices="/models/kokoro-int8-multi-lang-v1_1/voices.bin"))]
    assert speaker_choices(settings) == []


def test_hermes_settings_are_separate_editable_and_keep_machine_credentials_read_only(desk, tmp_path):
    import tomllib
    from pathlib import Path
    from loadcoach.settings import Changes, said

    browser, switch, users = desk
    models = Models(str(tmp_path / "loadcoach.db"))
    for name in ("small", "alternate", "not-allowed"):
        models.put(name, {"gpus": 0, "flags": {"model": name + ".gguf"}})
    users.change("jordan", own={"models": ["small", "alternate"]})
    store = switch.front.state.front_settings.changes
    compute_store = Changes(str(tmp_path / "loadcoach.db"))
    original = tomllib.loads(Path("settings/defaults/voice-gateway.toml").read_text())
    original["hermes"] = dict(model="small", provider_key_file="/keys/hermes.key", python="/venv/python")
    store.seen("voice-gateway", said(original, original, {}))
    compute_store.seen("voice", said({"profiles": {"default": {"model": "small"}}}, {}, {}))
    sign_in(browser, "jordan")
    page = browser.get("/system/voice?open=voice-gateway").text
    assert 'name="value-hermes_model" class="form-select"' in page
    assert 'Gateway default — small' in page
    assert '<option value="alternate">alternate</option>' in page
    assert '<option value="not-allowed"' not in page
    assert 'name="value-hermes_system_prompt"' in page
    assert 'name="value-hermes"' not in page
    assert 'Direct conversations' in page and 'Hermes agent' in page
    saved = browser.post("/settings/voice-gateway", data={"value-hermes_model": "alternate",
        "value-hermes_system_prompt": 'You are Jeeves.\nSay "sir".', "value-hermes_context_tokens": "4096",
        "value-hermes_max_turns": "4", "value-hermes_max_output_tokens": "256", "value-hermes_reasoning_effort": "low",
        "value-hermes": 'python = "/evil/python"'})
    assert saved.status_code == 200 and 'Saved.' in saved.text
    changed = store.of("voice-gateway")
    assert changed == dict(hermes_model="alternate", hermes_system_prompt='You are Jeeves.\nSay "sir".',
                          hermes_context_tokens=4096, hermes_max_turns=4, hermes_max_output_tokens=256,
                          hermes_reasoning_effort="low")
    assert compute_store.of("voice") == {}
    for field, value in (("model", "not-allowed"), ("max_turns", "0"), ("context_tokens", "false"),
                         ("max_output_tokens", "-1"), ("reasoning_effort", "unknown")):
        assert browser.post("/settings/voice-gateway", data={"value-hermes_" + field: value}).status_code == 400
        assert store.of("voice-gateway") == changed
    assert browser.post("/settings/voice-gateway", data={"reset": "hermes_model"}).status_code == 200
    assert "hermes_model" not in store.of("voice-gateway")
    assert store.of("voice-gateway")["hermes_max_turns"] == 4


def test_a_model_file_is_found_at_hugging_face_and_brought_down_from_the_site(desk, tmp_path, monkeypatch):
    monkeypatch.setattr("loadcoach.hub.PIECE", 64)  # small pieces, so that a small file comes in several
    browser, switch, _ = desk
    there, fast = switch.hub, tmp_path / "fast"
    fast.mkdir()
    # No places are named in this LoadCoach, so the one place is the folder its one model is in.
    Models(str(tmp_path / "loadcoach.db")).put("stays", {"gpus": 1, "flags": {"model": gguf(fast / "stays.gguf", general__name="Stays"), "ctx-size": 2048}})
    target = fast / "acme--tiny-GGUF" / "tiny-q4.gguf"

    def until(wanted):
        deadline = time.monotonic() + 5
        while not wanted() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert wanted()

    sign_in(browser, "pat")
    assert browser.get("/hub?q=tiny").status_code == 403 and "acme" not in browser.get("/hub?q=tiny").text
    browser.post("/sign-out")
    sign_in(browser, "jordan")
    assert 'Find a model file at Hugging Face</a>' not in browser.get("/files?complete=1").text and 'id="downloads"' in browser.get("/files").text
    assert 'href="/hub">HuggingFace</a>' in browser.get("/files").text

    found = browser.get("/hub?q=tiny").text
    assert '<h1 class="page-title wrap">HuggingFace</h1>' in found
    assert 'href="/hub?repository=acme/tiny-GGUF">acme/tiny-GGUF</a>' in found and "Nothing found" in browser.get("/hub?q=llama").text
    files = browser.get("/hub?q=acme/tiny-GGUF").text  # a repository typed out goes straight to its files
    assert 'name="file" value="tiny-q4.gguf"' in files and "and the other 1 of its 2 parts" in files and f'<option value="{fast}">' in files
    none = browser.get("/hub?repository=acme/none")
    assert none.status_code == 403 and "no such repository" in none.text
    unticked = browser.post("/hub", data={"repository": there.repository, "place": str(fast)})
    assert unticked.status_code == 400 and "Tick the files you want." in unticked.text and 'value="tiny-q4.gguf"' in unticked.text

    # It comes down, is stopped, and is gone on with. The list of downloads shows each step and has the button for the next.
    there.slow = 0.02
    started = browser.post("/hub", data={"repository": there.repository, "file": ["tiny-q4.gguf"], "place": str(fast)})
    assert "1 file, " in started.text and f"coming down to {fast}." in started.text and str(started.url).split("?")[0].endswith("/hub")
    until(lambda: 'value="stop"' in browser.get("/downloads").text)
    assert "a second" in browser.get("/downloads").text and "acme/tiny-GGUF" in browser.get("/downloads").text
    browser.post("/downloads", data={"do": "stop", "path": str(target)})
    until(lambda: 'value="continue"' in browser.get("/downloads").text)
    assert "stopped" in browser.get("/downloads").text and not target.exists()
    there.slow = 0
    browser.post("/downloads", data={"do": "continue", "repository": there.repository, "file": "tiny-q4.gguf", "place": str(fast)})
    until(target.exists)
    assert target.read_bytes() == there.files["tiny-q4.gguf"] and "tiny-q4.gguf" not in browser.get("/downloads").text

    # What is here already is marked and cannot be ticked. It is on Files, to be added as a model.
    again = browser.get("/hub?repository=acme/tiny-GGUF").text
    assert f"in {fast}</span>" in again and 'value="tiny-q4.gguf" type="checkbox" class="form-check-input" disabled' in again
    assert "tiny-q4.gguf</a>" in browser.get("/files?complete=1").text
    refused = browser.post("/downloads", data={"do": "discard", "path": str(target)})
    assert "There is no unfinished download to that path." in refused.text


def test_the_cards_show_their_room_and_a_model_page_what_the_model_measured(live_desk, tmp_path):
    browser, compute, users = live_desk
    jordan = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("jordan")}, timeout=30)
    kept = Models(str(tmp_path / "loadcoach.db"))
    sign_in(browser, "jordan")
    assert "14.6 GiB free for a model, of 15.6 GiB" in browser.get("/?part=status").text  # an empty card of 16000 MiB: all of it but the margin of 1024
    assert browser.get("/models?complete=1").text.count("Not measured") == 4 and browser.get("/models/small").text.count("not measured yet") == 2

    # A model that has loaded was measured. Status shows it on its card with that figure, and the room that is left.
    assert jordan.post("/v1/chat/completions", json={"model": "small"}).status_code == 200
    home = browser.get("/?part=status").text
    assert "11.7 GiB free for a model, of 15.6 GiB" in home and '<a href="/models/small">small</a>' in home and "2.9 GiB VRAM" in home  # 3000 MiB
    assert "2.9 GB" in browser.get("/models?complete=1").text and browser.get("/models?complete=1").text.count("Not measured") == 2
    page = browser.get("/models/small").text
    # Its page says it at the top, among what is known of the model: at its minimum and its maximum context, which are one here.
    assert page.count('2.9 GB <small class="text-secondary">at 2,048 tokens</small>') == 2 and 'value="forget-measure"' in page

    forgotten = browser.post("/models/small", data={"do": "forget-measure"})
    assert "Forgot what small measured." in forgotten.text and forgotten.text.count("not measured yet") == 2 and 'value="forget-measure"' not in forgotten.text and kept.measure("small") is None

    # The box that keeps a model's cards to itself is one of its settings.
    browser.post("/models/tiny", data={"file": "tiny.gguf", "ctx": "2048", "cards": "1", "alone": "yes", "flag": ["mib"], "value": ["1000"]})
    assert kept.stored("tiny") == {"flags": {"model": "tiny.gguf", "ctx-size": 2048, "mib": 1000}, "gpus": 1, "alone": True}
    assert 'name="alone" value="yes" type="checkbox" class="form-check-input" checked' in browser.get("/models/tiny").text
    assert jordan.post("/v1/chat/completions", json={"model": "tiny"}).status_code == 200
    home = browser.get("/?part=status").text
    assert "<td>the whole card" in home and "0 MiB free for a model, of 15.6 GiB" in home and "alone</small>" in browser.get("/models?complete=1").text


def test_the_models_pages_show_a_fingerprint_and_every_one_ever_seen_can_be_read_back(live_desk, tmp_path):
    browser, compute, users = live_desk
    jordan = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("jordan")}, timeout=30)
    sign_in(browser, "jordan")
    deadline = time.monotonic() + 5
    while "file being read" in browser.get("/models?part=list").text and time.monotonic() < deadline:  # the files are read through apart from any request
        time.sleep(0.05)
    kept = Models(str(tmp_path / "loadcoach.db"))
    mark, nothing = kept.fingerprint(kept.get("small")), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # the SHA-256 of an empty file, which small.gguf is

    listed = browser.get("/models?complete=1").text
    assert f'<a href="/fingerprints?start={mark}"><code>{mark[:12]}</code></a>' in listed and "llama-server on compute is 1 (fake)" in listed
    page = browser.get("/models/small").text
    assert f"<code>{mark}</code>" in page and f"<code>{nothing}</code>" in page and "1 (fake)" in page

    # An answer says which model gave it, and so does the page of the request.
    answer = jordan.post("/v1/chat/completions", json={"model": "small"})
    assert answer.headers["x-loadcoach-fingerprint"] == mark
    asked = browser.get(f"/requests/{answer.headers['x-loadcoach-request-id']}").text
    assert f"<code>{mark[:12]}</code>" in asked and "1 (fake)" in asked

    everything = browser.get("/fingerprints").text
    assert f"<code>{mark[:12]}</code>" in everything and "small.gguf" in everything and "tiny.gguf" in everything
    one = browser.get("/fingerprints?start=" + mark[:12]).text
    assert f"<code>{mark}</code>" in one and f"<code>{nothing}</code>" in one and "Its settings, as they were used" in one and "ctx-size" in one
    assert "No fingerprint begins so." in browser.get("/fingerprints?start=zzzz").text

    # The model is removed and its file deleted. What the fingerprint stood for can still be read.
    browser.post("/models/small", data={"do": "remove"})
    (tmp_path / "small.gguf").unlink()
    after = browser.get("/fingerprints?start=" + mark).text
    assert f"<code>{mark}</code>" in after and "small.gguf" in after and "/models/small" not in browser.get("/models?part=list").text
    browser.post("/sign-out")
    sign_in(browser, "pat")
    assert browser.get("/fingerprints").status_code == 403


def test_a_button_measures_every_model_that_has_no_measure(live_desk):
    browser, _, _ = live_desk
    sign_in(browser, "jordan")
    assert 'value="start">Measure the 2 models needing measurements</button>' in browser.get("/models?complete=1").text
    pressed = browser.post("/measure-models", data={"do": "start"})
    assert "Measuring the models that have no measure." in pressed.text
    deadline = time.monotonic() + 8
    while "The last measuring" not in browser.get("/models?part=list").text and time.monotonic() < deadline:
        time.sleep(0.05)
    after = browser.get("/models?part=list").text
    assert "2 measured, 0 left. Every model was tried." in after and "not yet" not in after and 'value="start"' not in after
    assert "2.9 GB" in after and "1.0 GB" in after  # what small and tiny use, as the stand-in for nvidia-smi says
    assert "Asked for the measuring to stop." in browser.post("/measure-models", data={"do": "stop"}).text  # nothing is under way, and saying so does no harm
    browser.post("/sign-out")
    sign_in(browser, "pat")
    assert browser.post("/measure-models", data={"do": "start"}).status_code == 403


def test_chat_upload_form_previews_text_and_keeps_failed_input(live_desk):
    import base64
    browser, compute, users = live_desk
    sign_in(browser, "pat")
    assert "data-files" in browser.get("/chat").text
    files = json.dumps([{"name": "<notes>.txt", "data": base64.b64encode(b"<script>alert(1)</script> cedar").decode()}])
    sent = browser.post("/chat", data={"content": "Read this", "model": "small", "attachments": files})
    ref = str(sent.url).rsplit("/", 1)[-1]
    until = time.monotonic() + 10
    while "data-stream" in (page := browser.get(f"/chat/{ref}").text) and time.monotonic() < until:
        time.sleep(.05)
    assert "&lt;notes&gt;.txt" in page and "&lt;script&gt;alert(1)&lt;/script&gt; cedar" in page
    assert "<script>alert(1)</script>" not in page
    failed = browser.post("/chat", data={"content": "Read this", "model": "missing", "attachments": files})
    assert failed.status_code == 400 and "&lt;notes&gt;.txt" in failed.text and "Read this</textarea>" in failed.text


def test_chat_comparison_has_two_columns_and_shared_followups(live_desk):
    browser, compute, users = live_desk
    sign_in(browser, "pat")
    token = {"authorization": "Bearer " + users.new_key("jordan")}
    original = httpx.get(compute + "/loadcoach/models", headers=token).json()["models"]
    # The live fixture supplies one model; register a second using that fixture's file.
    settings = next(m for m in original if m["name"] == "small")
    body = {k: settings[k] for k in ["flags", "gpus"] if k in settings}
    made = httpx.post(compute + "/loadcoach/models/second", headers=token, json=body)
    assert made.status_code == 201, made.text
    page = browser.post("/chat", data={"content": "hello", "model": "small", "compare_model": "second"})
    ref = str(page.url).rsplit("/", 1)[-1]
    until = time.monotonic() + 10
    while "data-stream" in (page := browser.get(f"/chat/{ref}").text) and time.monotonic() < until:
        time.sleep(.05)
    assert 'class="chat-main compare"' in page and page.count('class="turn comparison-answer frame"') == 2
    assert "Ask both again" in page and 'Models: small · second' in page
    assert browser.post(f"/chat/{ref}", data={"do": "say", "content": "next"}).status_code == 200


def test_adapters_and_projectors_are_listed_with_the_model_each_is_for_and_put_onto_one_from_lists(desk, tmp_path):
    browser, _, _ = desk
    folder = tmp_path / "models"
    folder.mkdir()
    base = gguf(folder / "base.gguf", general__name="Base", general__architecture="tiny", tiny__context_length=4096)
    # A file says itself what it is, and which model it was made for: an adapter by the name of the model it was trained on, a projector by carrying its model's name.
    terse = gguf(folder / "terse.gguf", general__name="Terse", general__type="adapter", general__architecture="tiny", general__base_model__0__name="some/folder/Base")
    stray = gguf(folder / "stray.gguf", general__name="Stray", general__type="adapter", general__architecture="tiny", general__base_model__0__name="Another Model")
    eyes = gguf(folder / "eyes.gguf", general__name="Base", general__type="mmproj", general__architecture="clip")
    kept = Models(str(tmp_path / "loadcoach.db"))
    kept.put("base", {"gpus": 1, "flags": {"model": base, "ctx-size": 2048}})
    sign_in(browser, "jordan")

    # Files has a part for each kind. A part's row says which model it was made for and which model here that is, or that there is none.
    listed = browser.get("/files?complete=1").text
    adapters, projectors = listed[listed.index('id="files-adapters"') : listed.index('id="files-projectors"')], listed[listed.index('id="files-projectors"') :]
    assert listed.index('id="files-models"') < listed.index("base.gguf</a>") < listed.index('id="files-adapters"') and "terse.gguf" not in listed[: listed.index('id="files-adapters"')]
    assert "<td>Base</td>" in adapters and '<a href="/models/base">base</a>' in adapters and ">Make a model</a>" in adapters
    assert "<td>Another Model</td>" in adapters and adapters.count("no model for it here") == 1
    assert "eyes.gguf</a>" in projectors and '<a href="/models/base">base</a>' in projectors and ">Make a model</a>" in projectors and "no model for it here" not in projectors

    # A model is made with an adapter from the adapter's own page. The model its file names comes first in the list, and a file that names none here is marked.
    page, other = browser.get("/file?path=" + terse).text, browser.get("/file?path=" + stray).text
    assert "Make a model with this adapter" in page and '<option value="base">base</option>' in page and 'name="short" class="form-control" value="terse"' in page
    assert "base · not what this file says it is for" in other and "This file says it was made for Another Model." in other
    unnamed = browser.post("/file", data={"path": terse, "do": "adapt", "model": "base", "short": "terse", "name": ""})
    assert unnamed.status_code == 400 and "Give the new model a name." in unnamed.text
    made = browser.post("/file", data={"path": terse, "do": "adapt", "model": "base", "short": "terse", "name": "base-terse"})
    assert "Made base-terse: base with the adapter terse." in made.text and str(made.url).split("?")[0].endswith("/models/base-terse")
    # A copy of the model with the adapter attached. The model it was made from is as it was.
    assert kept.stored("base-terse") == {"flags": {"model": base, "ctx-size": 2048}, "gpus": 1, "adapters": {"terse": {"file": terse}}}
    assert kept.stored("base") == {"gpus": 1, "flags": {"model": base, "ctx-size": 2048}}
    assert "with terse</small>" in browser.get("/models?complete=1").text and '<a href="/models/base-terse">base-terse</a>' in browser.get("/files?complete=1").text  # the file is one of the model's now

    # On a model's page an adapter is chosen from the adapters there are, not typed. A row with its word emptied is an adapter taken away.
    assert f'<option value="{terse}" selected>{terse} · metadata names agree</option>' in made.text and f'<option value="{stray}" >{stray} · says it is for Another Model</option>' in made.text
    form = {"file": base, "ctx": "2048", "cards": "1"}
    browser.post("/models/base-terse", data={**form, "adapter": ["terse", "half", ""], "adapter_file": [terse, stray, ""], "adapter_scale": ["", "0.5", ""]})
    assert kept.stored("base-terse")["adapters"] == {"terse": {"file": terse}, "half": {"file": stray, "scale": 0.5}}
    wrong = browser.post("/models/base-terse", data={**form, "adapter": ["a name"], "adapter_file": [terse], "adapter_scale": [""]})
    assert wrong.status_code == 400 and "A name is letters, digits, dots and dashes." in wrong.text and 'value="a name"' in wrong.text  # what was typed is still there to mend
    browser.post("/models/base-terse", data={**form, "adapter": ["", ""], "adapter_file": [terse, stray], "adapter_scale": ["", ""]})
    assert "adapters" not in kept.stored("base-terse")

    # A model that sees is made from the projector's page: a copy with the projector, under a name of its own. The model it was made from stays blind.
    unnamed = browser.post("/file", data={"path": eyes, "do": "project", "model": "base", "name": ""})
    assert unnamed.status_code == 400 and "Give the new model a name." in unnamed.text
    seeing = browser.post("/file", data={"path": eyes, "do": "project", "model": "base", "name": "base-vision"})
    assert "Made base-vision: base with the projector eyes.gguf" in seeing.text and str(seeing.url).split("?")[0].endswith("/models/base-vision")
    assert kept.stored("base-vision") == {"flags": {"model": base, "ctx-size": 2048, "mmproj": eyes}, "gpus": 1} and "mmproj" not in kept.stored("base")["flags"]
    assert "vision</small>" in browser.get("/models?complete=1").text
    # On a model's page the projector is chosen from the projectors there are, or taken away. It is not a flag to type.
    assert f'<option value="{eyes}" selected>{eyes} · metadata names agree</option>' in seeing.text and 'name="flag" class="form-control" value="mmproj"' not in seeing.text
    browser.post("/models/base-vision", data={**form, "projector": ""})
    assert "mmproj" not in kept.stored("base-vision")["flags"]


def test_the_batches_page_shows_how_far_each_has_got_and_gives_its_answers(live_desk):
    browser, compute, users = live_desk
    pat = httpx.Client(base_url=compute, headers={"authorization": "Bearer " + users.new_key("pat")}, timeout=30)
    line = lambda number, body: json.dumps({"custom_id": f"q{number}", "method": "POST", "url": "/v1/chat/completions", "body": body})  # noqa: E731
    slow = {"model": "small", "sleep": 1.5, "messages": [{"role": "user", "content": "hello"}]}
    file = pat.post("/v1/files", data={"purpose": "batch"}, files={"file": ("in.jsonl", "\n".join([line(0, {**slow, "sleep": 0}), line(1, slow), line(2, slow)]))}).json()
    batch = pat.post("/v1/batches", json={"input_file_id": file["id"], "endpoint": "/v1/chat/completions", "completion_window": "24h"}).json()

    sign_in(browser, "pat")
    assert 'href="/batches"' in browser.get("/batches").text
    deadline = time.monotonic() + 5
    while "1 of 3" not in browser.get("/batches?part=list").text and time.monotonic() < deadline:
        time.sleep(0.05)
    listed = browser.get("/batches").text
    assert f"<code>{batch['id']}</code>" in listed and "being answered" in listed and "1 of 3" in listed and 'value="cancel"' in listed and "<td>pat</td>" in listed
    assert "<html" not in browser.get("/batches?part=list").text and "/v1/files" in listed  # the list alone asks for itself again; how to hand one in is on the page

    cancelled = browser.post("/batches", data={"batch": batch["id"], "do": "cancel"})
    assert "Cancelled. The answers there were are kept" in cancelled.text
    while "cancelled" not in browser.get("/batches?part=list").text and time.monotonic() < deadline + 5:
        time.sleep(0.05)
    after = browser.get("/batches?part=list").text
    output = pat.get(f"/v1/batches/{batch['id']}").json()["output_file_id"]
    assert f'href="/batches/{output}">Answers</a>' in after and 'value="cancel"' not in after
    answers = browser.get(f"/batches/{output}")
    assert answers.headers["content-disposition"] == f'attachment; filename="{output}.jsonl"' and json.loads(answers.text)["custom_id"] == "q0"

    # Someone who may see everyone's requests sees the batch, and how far it got. Its answers are pat's.
    browser.post("/sign-out")
    sign_in(browser, "jordan")
    others = browser.get("/batches").text
    assert f"<code>{batch['id']}</code>" in others and "Answers</a>" not in others and browser.get(f"/batches/{output}").status_code == 404
    browser.post("/sign-out")
    sign_in(browser, "cam")
    assert "No batches." in browser.get("/batches").text


def test_backup_page_creates_a_paired_verified_recovery_set(desk):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    page = browser.get("/backups")
    assert page.status_code == 200
    assert "Last verified recovery point" in page.text and "Key and archive protection" in page.text
    assert "Backups" in page.text and "No backups have been made" in page.text

    created = browser.post("/backups/create")
    assert created.status_code == 200
    assert "created and restored into isolated folders successfully" in created.text
    assert created.text.count("verified") >= 3
    assert "Archive / data" in created.text and "data before compression" in created.text
    preview_link = re.search(r'href="(/backups/[^"]+)">Restore preview</a>', created.text)
    assert preview_link
    preview = browser.get(preview_link.group(1)).text
    assert "Services that must be stopped" in preview
    assert "weightroom.service" in preview and "loadcoach.service" in preview
    assert "users <strong>" in preview and "conversations <strong>" in preview and "benchmark runs <strong>" in preview
    assert "Verified database stores" in preview and "SQLite online backup API" in preview


def test_database_page_lists_front_tables_and_exports_restorable_sql(desk, tmp_path):
    browser, _, _ = desk
    sign_in(browser, "jordan")
    page = browser.get("/databases")
    assert page.status_code == 200
    assert "Database storage" in page.text and "PostgreSQL" in page.text and "supported" in page.text
    assert "Offline migration available" in page.text and "No migration profiles are configured" in page.text
    assert "chat.db" in page.text and "conversations" in page.text and "Dump SQL" in page.text
    assert "KiB" in page.text
    assert 'class="table table-sm align-middle sortable mb-0"' in page.text
    assert 'data-sort="text">Table</th>' in page.text
    assert page.text.count('data-sort="value"') >= 3
    assert re.search(r'<td class="text-end" data-value="\d+">', page.text)

    dumped = browser.get("/databases/front/chat.db/dump?table=conversations")
    assert dumped.status_code == 200 and "CREATE TABLE conversations" in dumped.text
    restored = sqlite3.connect(tmp_path / "chat-table.db")
    restored.executescript(dumped.text)
    assert restored.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    restored.close()


def test_store_health_permissions_backup_age_and_compute_outage(desk):
    browser, compute, _ = desk
    sign_in(browser, "pat")
    assert browser.get("/databases").status_code == 403
    assert "Store health and backup age" not in browser.get("/").text
    sign_in(browser, "jordan")
    page = browser.get("/databases")
    assert "Store health and backup age" in page.text and "No matching verified archive" in page.text
    assert "default_weightroom_chat" in page.text and "weightroom.chat" in page.text
    assert browser.post("/backups/create").status_code == 200
    page = browser.get("/databases")
    assert '>fresh</span>' in page.text and "hours old" in page.text
    compute.off = True
    page = browser.get("/databases")
    assert page.status_code == 200
    assert "Compute is not answering" in page.text and "default_weightroom_chat" in page.text


def test_machine_storage_signals_are_on_database_page(desk_with_machine):
    browser, compute, machine, _, _ = desk_with_machine
    sign_in(browser, "jordan")
    page = browser.get("/machine")
    assert page.status_code == 200 and "Store health and backup age" not in page.text
    compute.off = True
    page = browser.get("/machine")
    assert page.status_code == 200 and "Store health and backup age" not in page.text
    compute.off = False  # Keep an authentication authority available; both down requires a new offline sign-in.
    machine.off = True
    page = browser.get("/machine")
    assert page.status_code == 503 and "Store health and backup age" not in page.text
    page = browser.get("/databases")
    assert page.status_code == 200 and "Store health and backup age" in page.text
    assert "default_weightroom_chat" in page.text


def test_multimedia_gpu_names_are_escaped_in_attribute(desk, tmp_path):
    browser, _, _ = desk
    file = gguf(tmp_path / 'image.gguf')
    Models(str(tmp_path / 'loadcoach.db')).put('escaped-image', {
        'engine': 'sdcpp', 'gpus': ['CUDA0'], 'flags': {'model': file, 'ctx-size': 0},
        'sdcpp': {'family': 'sdxl', 'components': {}, 'defaults': {}, 'limits': {}, 'host_ram_mib': 8192}})
    sign_in(browser, 'jordan')
    page = browser.get('/models/escaped-image')
    assert page.status_code == 200
    assert 'data-gpus="[&#34;CUDA0&#34;]"' in page.text
    assert 'name="node_options"' not in page.text


def test_initial_parallel_context_is_capped_to_total():
    from engines.llama_cpp.form import parallel_fields
    assert parallel_fields({'ctx-size': 262114}, 262144)['context'] == 262114
    saved = parallel_fields({'ctx-size': 100, 'kv-unified-per-slot': 200})
    assert saved['exceeds_total'] and saved['context'] == 200
