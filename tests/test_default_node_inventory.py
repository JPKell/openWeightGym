"""What still depends on the default compute node (Phase 0 of docs/peer-nodes-plan.md).

Two ratchets, each against a checked-in list in tests/fixtures:

- default_node_fallthrough.txt: every console request that turnstile sends to the default node
  only because nothing else routed it. Later phases route these paths; the list must shrink to
  nothing, and a new entry fails the test.
- compute_literals.json: how many times each source file names the node "compute". A new or
  extra reference fails; a removed one asks for the list to be lowered, so it never grows back.

After a deliberate change, rewrite both lists with OWG_UPDATE_INVENTORY=1 and review the diff.
"""

import json
import os
import re
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from front_helpers import turnstile_to
from loadcoach.groups import ABILITIES, OWN_KEY, Group
from loadcoach.users import Users
from turnstile.fallthrough import Fallthrough, pattern

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FALLTHROUGH = FIXTURES / "default_node_fallthrough.txt"
LITERALS = FIXTURES / "compute_literals.json"
UPDATE = os.environ.get("OWG_UPDATE_INVENTORY") == "1"
HEADER = """# Console requests turnstile sends to the default compute node only because nothing routed them.
# METHOD path port. Written by tests/test_default_node_inventory.py with OWG_UPDATE_INVENTORY=1.
# docs/peer-nodes-plan.md routes each of these to a named node; this list must shrink to nothing.
"""

# The programs that ask turnstile for work: the console and its in-process helpers.
CALLERS = ["weightroom", "mirrorwall", "soundroom", "benchmarks"]
# Where the node name "compute" is counted.
SOURCES = ["turnstile", "weightroom", "loadcoach", "benchmarks", "mirrorwall", "soundroom",
           "spotter", "db", "voice", "toollocker"]
COMPUTE = re.compile(r"""(["'])compute\1""")
# A path literal. An f-string placeholder is matched whole, so quotes inside it don't end the path.
PATH = r"""f?["'](?P<path>/(?:loadcoach|v1)/(?:\{[^{}]*\}|[^?#\s{"'])*)"""
METHOD_PATH = re.compile(r"""["'](?P<method>GET|POST|PUT|PATCH|DELETE)["']\s*,\s*""" + PATH)
FETCH_PATH = re.compile(r"""\bfetch\(\s*request\s*,\s*person\s*,\s*""" + PATH)
# The console answers these itself, in process, without turnstile (weightroom/app.py `ask`).
IN_PROCESS = ("/loadcoach/conversations", "/loadcoach/collections", "/loadcoach/workflows")


def console_requests() -> set[tuple[str, str]]:
    """Every request the console's Python sends to turnstile with a literal path.

    A call that names its method (`ask("POST", "/loadcoach/…")`) gives both; `fetch(request,
    person, path)` is always a GET. An f-string placeholder, or the part a path ending in a slash
    has added to it, becomes `sample`, so each request has the shape a real one has. Paths the
    console answers itself are left out.
    """
    def shaped(path: str) -> str:
        path = re.sub(r"\{[^{}]*\}", "sample", path)
        return path + "sample" if path.endswith("/") else path  # the rest is added with +, as in "/loadcoach/models/" + name

    found = set()
    for folder in CALLERS:
        for source in sorted((ROOT / folder).rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            found |= {(one["method"], shaped(one["path"])) for one in METHOD_PATH.finditer(text)}
            found |= {("GET", shaped(one["path"])) for one in FETCH_PATH.finditer(text)}
    return {(method, path) for method, path in found if not path.startswith(IN_PROCESS)}


def compute_literals() -> dict[str, int]:
    """How many times each source file names the node "compute", by repository path."""
    counts = {}
    for folder in SOURCES:
        for source in sorted((ROOT / folder).rglob("*")):
            if source.suffix in (".py", ".html", ".js") and "__pycache__" not in source.parts:
                found = len(COMPUTE.findall(source.read_text(encoding="utf-8")))
                if found:
                    counts[source.relative_to(ROOT).as_posix()] = found
    return counts


def test_routes_are_patterned_with_record_ids_replaced():
    assert pattern("/loadcoach/tasks/batch_c9fd052accf99d11403448b4/probe") == "/loadcoach/tasks/{id}/probe"
    assert pattern("/loadcoach/stop/12") == "/loadcoach/stop/{id}"
    assert pattern("/v1/models/wl9-smollm-135m-import") == "/v1/models/wl9-smollm-135m-import"
    assert pattern("/loadcoach/backups/" + "a" * 32) == "/loadcoach/backups/{id}"


def test_each_route_is_printed_once_and_the_memory_is_bounded():
    said = []
    seen = Fallthrough(limit=2, say=lambda text, **_: said.append(text))
    seen.note("GET", "/loadcoach/stop/1", 8442)
    seen.note("GET", "/loadcoach/stop/2", 8442)
    seen.note("POST", "/loadcoach/tasks", 8442)
    seen.note("GET", "/loadcoach/peek/3", 8442)  # over the limit: neither remembered nor printed
    assert said == ["turnstile: default-node fallthrough GET /loadcoach/stop/{id} on port 8442",
                    "turnstile: default-node fallthrough POST /loadcoach/tasks on port 8442"]
    assert seen.seen == {("GET", "/loadcoach/stop/{id}", 8442): 2, ("POST", "/loadcoach/tasks", 8442): 1}
    assert seen.routes() == {"GET /loadcoach/stop/{id} 8442", "POST /loadcoach/tasks 8442"}


def test_console_requests_that_fall_through_to_the_default_node(tmp_path):
    """Send every console request through turnstile and list those that reach the fallthrough.

    The default node here only records what reaches it. Its answers carry no proof, so
    turnstile refuses them, which is enough: the fallthrough is noted before anything is sent.
    """
    async def anything(request):
        return JSONResponse({})

    compute = httpx.ASGITransport(app=Starlette(routes=[Route("/{path:path}", anything, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])]))
    (tmp_path / "data").mkdir()
    users = Users(str(tmp_path / "data" / "accounts.db"),
                  {"admin": Group(can=frozenset(ABILITIES) - OWN_KEY - {"superuser"}, max_sensitivity="restricted")})
    users.add("jordan", "admin")
    # The switches as the live front has them (2026-11-12): console settings on the front, ToolLocker
    # on the front, and task creation still left to compute (front_tasks unset).
    front = turnstile_to(tmp_path, users, {8442: compute}, wait=0, front_settings=True, toollocker_url="http://127.0.0.1:9443")
    headers = {"authorization": "Bearer " + users.new_key("jordan"), "x-turnstile-no-wake": "1"}
    with TestClient(front, base_url="http://front:8442", raise_server_exceptions=False) as api:
        for method, path in sorted(console_requests()):
            api.request(method, path, headers=headers, **({"json": {}} if method != "GET" else {}))
        found = sorted(front.state.fallthrough.routes())
    if UPDATE:
        FALLTHROUGH.write_text(HEADER + "".join(line + "\n" for line in found))
    expected = [line for line in FALLTHROUGH.read_text().splitlines() if line and not line.startswith("#")]
    added, gone = sorted(set(found) - set(expected)), sorted(set(expected) - set(found))
    assert not added, f"New requests fall through to the default node; route them to a named node: {added}"
    assert not gone, f"These no longer fall through; remove them from {FALLTHROUGH.name} (OWG_UPDATE_INVENTORY=1): {gone}"


def test_no_new_references_to_the_compute_node_name():
    found = compute_literals()
    if UPDATE:
        LITERALS.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
    allowed = json.loads(LITERALS.read_text())
    grown = {name: (allowed.get(name, 0), count) for name, count in found.items() if count > allowed.get(name, 0)}
    shrunk = {name: (count, found.get(name, 0)) for name, count in allowed.items() if found.get(name, 0) < count}
    assert not grown, f'New "compute" node-name references (allowed, found); name the node instead: {grown}'
    assert not shrunk, f"Fewer references than allowed (allowed, found); lower {LITERALS.name} (OWG_UPDATE_INVENTORY=1): {shrunk}"
