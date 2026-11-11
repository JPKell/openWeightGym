"""Front's inventory of compute machines: what each last said of itself, and how long ago.

Nothing here wakes a machine or carries a request. A machine that has not answered since
turnstile started is `unknown`; one that answered, but not within three polls, is `not answering`
and its last facts are shown with their age.
"""
import asyncio
import json
import time

from spotter.app import gathered, series

READY, SILENT, UNKNOWN = "ready", "not answering", "unknown"


class PassiveReads:
    """Carry a page's no-wake read policy through its internal worker lookups."""

    def __init__(self, app, current):
        """Wrap an ASGI app; `current` is the context variable that holds the no-wake flag."""
        self.app, self.current = app, current

    async def __call__(self, scope, receive, send):
        """Mark a GET that asks for no wake while it runs, and clear the mark after."""
        headers = dict(scope.get("headers", []))
        token = self.current.set(scope["type"] == "http" and scope["method"] == "GET"
                                 and b"x-turnstile-no-wake" in headers)
        try:
            await self.app(scope, receive, send)
        finally:
            self.current.reset(token)


def logical_catalogue(found: dict[str, dict]) -> list[dict]:
    """One caller-visible model with explicit per-machine copies.

    Equal verified weights are one logical model only when each machine has at
    most one copy of those weights. Multiple aliases on one machine are
    intentionally left distinct because front cannot infer that adapters or
    other semantic settings are interchangeable.
    """
    rows = [(machine, model) for machine, result in found.items()
            for model in result.get("data", []) if model.get("owned_by") == "loadcoach"]
    counts = {}
    for machine, model in rows:
        content = model.get("content_sha256")
        if content:
            key = (content, model.get("engine_id", "llama_cpp"), machine)
            counts[key] = counts.get(key, 0) + 1
    grouped = {}
    for machine, model in rows:
        content = model.get("content_sha256")
        engine = model.get("engine_id", "llama_cpp")
        safe = bool(content and all(counts.get((content, engine, name), 0) <= 1 for name in found))
        key = ("content", content, engine) if safe else ("alias", model.get("id"), content, engine, machine if not content else "")
        grouped.setdefault(key, []).append((machine, model))
    listed = []
    for choices in grouped.values():
        choices.sort(key=lambda item: (item[0] != "compute", item[0], item[1].get("id", "")))
        machine, canonical = choices[0]
        locations = [{"machine": name, **{key: model[key] for key in
                      ("id", "model_id", "fingerprint", "content_sha256", "embedding", "context_tokens", "build", "engine_id", "capabilities", "chat_template_caps") if key in model}}
                     for name, model in choices]
        listed.append({**canonical, "logical_id": "sha256:" + canonical["content_sha256"] if canonical.get("content_sha256") else "",
                       "machines": [one["machine"] for one in locations], "locations": locations})
    return sorted(listed, key=lambda model: (
        model.get("order", 10) if model.get("order") is not None else 10,
        model.get("id", "").casefold(), model.get("id", "")))


def placement_order(variants: list[tuple[str, dict]], reports: dict[str, dict]) -> list[tuple[str, dict]]:
    """Prefer an eligible resident, then a fit without eviction, then the shorter line.

    These are hints only: LoadCoach rechecks real memory and admits or refuses the request.
    """
    def priority(item):
        """Return the configured placement priority for a candidate compute location."""
        name, fact = item
        report = reports.get(name, {})
        free = sum(card.get("free", 0) for card in report.get("cards", []))
        waiting = report.get("waiting", 0) + report.get("running", 0)
        return (bool(fact.get("why_not")), not bool(fact.get("warm")), not bool(fact.get("resident")),
                fact.get("fit") != "fits now", len(fact.get("evictions") or []), waiting, -free, name)
    return sorted(variants, key=priority)


class Machines:
    """Configured compute nodes, cached availability and placement metadata."""
    def __init__(self, names, ask, every: float = 10.0, now=time.time, ask_telemetry=None, store=None):
        """`names` are the machines' names. `ask(name, path)` is the machine's answer, parsed, or None if it did not answer."""
        self.names, self.ask, self.every, self.now = list(names), ask, every, now
        self.ask_telemetry, self.store = ask_telemetry, store
        self.seen: dict[str, dict] = {}
        self.checked: set[str] = set()
        self.last_saved: dict[str, float] = {}
        self.last_pruned = 0.0
        self.known_hosts = {} if store is None else {
            fingerprint: {"hostname": hostname, "machines": json.loads(names), "last_seen": last_seen}
            for fingerprint, hostname, names, last_seen in store.execute(
                "SELECT fingerprint, hostname, names, last_seen FROM machine_hosts").fetchall()}
        self.lifecycle = {} if store is None else {
            name: {"state": state, "changed_at": at, "changed_by": by}
            for name, state, at, by in store.execute(
                "SELECT name, state, changed_at, changed_by FROM machine_lifecycle").fetchall()}

    def all_names(self) -> list[str]:
        """Include retained registrations so an uninstalled, offline node can be retired."""
        known = {name for host in self.known_hosts.values() for name in host["machines"]}
        return list(dict.fromkeys([*self.names, *sorted(known | self.lifecycle.keys())]))

    def state(self, name: str) -> str:
        """Read the operator's lifecycle choice independently of reachability."""
        return self.lifecycle.get(name, {}).get("state", "active")

    def enabled(self, name: str) -> bool:
        """Only configured, active nodes may receive new work or wake requests."""
        return name in self.names and self.state(name) == "active"

    @property
    def active_names(self) -> list[str]:
        """Configured nodes eligible for discovery and placement."""
        return [name for name in self.names if self.enabled(name)]

    def change_state(self, name: str, state: str, by: str) -> None:
        """Persist lifecycle on front, without contacting the node or deleting history."""
        if name not in self.all_names():
            raise ValueError("Unknown compute node.")
        if state not in ("active", "disabled", "retired"):
            raise ValueError("Choose active, disabled or retired.")
        if state == "active" and name not in self.names:
            raise ValueError("Pair or configure this node again before enabling it.")
        at = self.now()
        if self.store is not None:
            self.store.execute("INSERT INTO machine_lifecycle(name,state,changed_at,changed_by) VALUES(?,?,?,?) "
                               "ON CONFLICT(name) DO UPDATE SET state=excluded.state, changed_at=excluded.changed_at, changed_by=excluded.changed_by",
                               (name, state, at, by))
        self.lifecycle[name] = {"state": state, "changed_at": at, "changed_by": by}

    async def poll(self) -> None:
        """Ask each machine about itself once. A machine that does not answer keeps what it last said."""
        async def one(name):
            """Poll one compute node and retain its current availability metadata."""
            status, models, telemetry = await asyncio.gather(
                self.ask(name, "/loadcoach/status"), self.ask(name, "/v1/models"),
                self.ask_telemetry(name, "/spotter/now") if self.ask_telemetry else asyncio.sleep(0, result=None),
                return_exceptions=True)
            if self.state(name) == "retired":
                return  # a retirement during the poll must not publish another reading
            status = None if isinstance(status, Exception) else status
            models = None if isinstance(models, Exception) else models
            telemetry = None if isinstance(telemetry, Exception) else telemetry
            self.checked.add(name)
            if status is None and telemetry is None:
                if name in self.seen:
                    self.seen[name]["status_live"] = False
                return
            previous = self.seen.get(name, {})
            fingerprint = (status or {}).get("machine_fingerprint") or previous.get("fingerprint") or (telemetry or {}).get("machine_fingerprint")
            if telemetry and (not fingerprint or telemetry.get("machine_fingerprint") != fingerprint):
                telemetry = None  # never join a reading to a different host merely by its configured name
            if telemetry:
                telemetry = {key: telemetry[key] for key in ("at", "machine_fingerprint", "hostname", "cards", "processor", "memory", "disks")
                             if key in telemetry}
            if status is not None:
                memory = status.get("memory", {})
                last_telemetry = {key: previous[key] for key in ("telemetry", "telemetry_at") if key in previous} if previous.get("fingerprint") == fingerprint else {}
                previous = {"at": self.now(), "status_live": True, "fingerprint": fingerprint,
                            "hostname": status.get("hostname") or (telemetry or {}).get("hostname") or name,
                            "cards": [{"card": card, "total": (memory.get(card) or {}).get("total"),
                                       "free": (memory.get(card) or {}).get("free"),
                                       "others": (memory.get(card) or {}).get("others")}
                                      for card in status.get("model_cards", status.get("cards", memory))],
                            "loaded": [{"model": one["model"], "state": one["state"], "cards": one["gpus"],
                                        **{field: one[field] for field in ("locked", "mib", "rss_mib", "rss_peak_mib") if field in one}}
                                       for one in status.get("loaded", [])],
                            "system_memory": status.get("system_memory"), "cpu": status.get("cpu"),
                            "running": len(status.get("running", [])), "waiting": len(status.get("waiting", [])),
                            "busy": status.get("busy", 0), "draining": bool(status.get("draining")),
                            "models": sorted(({"name": one["id"], "content": one.get("content_sha256")}
                                              for one in (models or {}).get("data", []) if one.get("owned_by") == "loadcoach"), key=lambda one: one["name"]),
                            **last_telemetry}
            else:
                previous = {"at": None, "fingerprint": fingerprint, "hostname": (telemetry or {}).get("hostname") or name,
                            "cards": [], "loaded": [], "running": 0, "waiting": 0, "busy": 0,
                            "draining": False, "models": [], **previous}
                previous["status_live"] = False
            if telemetry:
                previous.update(telemetry=telemetry, telemetry_at=self.now())
            self.seen[name] = previous
            if self.store and fingerprint:
                known = self.known_hosts.get(fingerprint, {"machines": []})
                names = sorted({*known["machines"], name})
                self.store.execute("INSERT INTO machine_hosts (fingerprint, hostname, names, last_seen) VALUES (?, ?, ?, ?) "
                                   "ON CONFLICT(fingerprint) DO UPDATE SET hostname=excluded.hostname, names=excluded.names, last_seen=excluded.last_seen",
                                   (fingerprint, previous["hostname"], json.dumps(names), self.now()))
                self.known_hosts[fingerprint] = {"hostname": previous["hostname"],
                                                 "machines": names, "last_seen": self.now()}
            if telemetry and self.store and fingerprint and self.now() - self.last_saved.get(fingerprint, 0) >= 30:
                self.store.execute("INSERT INTO machine_telemetry (fingerprint, at, numbers) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                                   (fingerprint, telemetry["at"], json.dumps(series(telemetry))))
                self.last_saved[fingerprint] = self.now()
        names = [name for name in self.names if self.state(name) != "retired"]
        outcomes = await asyncio.gather(*(one(name) for name in names), return_exceptions=True)
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                print(f"turnstile: machine {name} poll failed: {outcome!r}", flush=True)
        if self.store and self.now() - self.last_pruned > 3600:
            self.store.execute("DELETE FROM machine_telemetry WHERE at < ?", (self.now() - 28 * 86400,))
            self.last_pruned = self.now()

    def report(self, include_retired: bool = False) -> list[dict]:
        """Every machine, with its last facts and their age. `ready` means it answered within three polls."""
        out = []
        for name in self.all_names() if include_retired else self.names:
            if not include_retired and self.state(name) == "retired":
                continue
            found = self.seen.get(name)
            age = None if found is None or found["at"] is None else max(0.0, self.now() - found["at"])
            state = UNKNOWN if age is None else READY if age <= 3 * self.every else SILENT
            telemetry_age = None if found is None or found.get("telemetry_at") is None else max(0.0, self.now() - found["telemetry_at"])
            out.append({"name": name, "state": state, "age": age, "telemetry_age": telemetry_age,
                        "lifecycle": self.state(name), "configured": name in self.names,
                        "accepting_work": self.enabled(name),
                        "lifecycle_changed": self.lifecycle.get(name),
                        **({k: v for k, v in found.items() if k != "at"} if found else {})})
        return out

    def available(self, name: str) -> bool | None:
        """Latest passive status result; None until the first monitor poll completes."""
        if self.state(name) == "retired":
            return False
        if name not in self.checked:
            return None
        found = self.seen.get(name, {})
        at = found.get("at")
        return bool(found.get("status_live") and at is not None and self.now() - at <= 3 * self.every)

    def hosts(self) -> list[dict]:
        """One card per physical host, even if it runs several LoadCoach instances."""
        grouped: dict[str, dict] = {}
        known_names = {name for one in self.known_hosts.values() for name in one["machines"]}
        for machine in self.report():
            if not machine.get("fingerprint") and machine["name"] in known_names:
                continue  # restored history is the known host, not a second unidentified card
            key = machine.get("fingerprint") or "unidentified:" + machine["name"]
            host = grouped.setdefault(key, {"fingerprint": machine.get("fingerprint"),
                                            "hostname": machine.get("hostname") or machine["name"],
                                            "machines": [], "state": "unknown", "telemetry": None, "age": None,
                                            "loaded": [], "models_unknown": False})
            host["machines"].append(machine["name"])
            if machine["state"] == READY and machine.get("status_live", False):
                host["loaded"].extend({**one, "machine": machine["name"]} for one in machine.get("loaded", []))
            else:
                host["models_unknown"] = True
            if machine["state"] == READY or (machine.get("telemetry_age") is not None and machine["telemetry_age"] <= 3 * self.every):
                host["state"] = READY
            elif host["state"] != READY and machine["state"] == SILENT:
                host["state"] = SILENT
            if machine.get("telemetry") and (host["age"] is None or machine["telemetry_age"] < host["age"]):
                host["telemetry"], host["age"] = machine["telemetry"], machine["telemetry_age"]
        for fingerprint, known in self.known_hosts.items():
            names = [name for name in known["machines"] if self.state(name) != "retired"]
            if not names:
                continue
            grouped.setdefault(fingerprint, {"fingerprint": fingerprint, "hostname": known["hostname"],
                                             "machines": names, "state": SILENT,
                                             "telemetry": None, "age": max(0.0, self.now() - known["last_seen"]),
                                             "loaded": [], "models_unknown": True})
        for host in grouped.values():
            host["disabled_nodes"] = [name for name in host["machines"] if self.state(name) == "disabled"]
        return list(grouped.values())

    def history(self, fingerprint: str, end: float, seconds: float, points: int) -> dict:
        """Read retained machine telemetry for the selected physical host."""
        if self.store is None:
            return gathered([], end - seconds, end, points)
        rows = self.store.execute("SELECT at, numbers FROM machine_telemetry WHERE fingerprint = ? AND at >= ? AND at <= ? ORDER BY at",
                                  (fingerprint, end - seconds, end)).fetchall()
        return gathered(rows, end - seconds, end, points)

    def models(self) -> list[dict]:
        """Each model once across the machines, by the SHA-256 of its weights file, with the names it goes by and where it is.

        A file not yet read through has no hash, so it stands alone as that machine's name for it: it
        is not taken to be the same as another machine's model of that name.
        """
        found: dict = {}
        for name, facts in self.seen.items():
            if not self.enabled(name):
                continue
            for one in facts["models"]:
                key = one["content"] or f"{name}:{one['name']}"
                entry = found.setdefault(key, {"content": one["content"], "names": set(), "machines": []})
                entry["names"].add(one["name"])
                entry["machines"].append(name)
        return sorted(({**entry, "names": sorted(entry["names"])} for entry in found.values()), key=lambda entry: entry["names"][0])

    async def run(self) -> None:
        """Poll configured compute nodes periodically within the existing service process."""
        while True:
            try:
                await self.poll()
            except Exception as problem:  # one bad answer never ends the inventory
                print(f"turnstile: machine poll failed: {problem!r}", flush=True)
            await asyncio.sleep(self.every)
