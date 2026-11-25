"""Re-qualifying image and video profiles that a runtime change left stale.

Evidence is bound to the native programs and the workflow contract (mirrorwall/engine.py). After
either changes, a model keeps what the earlier build proved, flagged as pending, until one run of
its own qualifies it again (Runner.image_evidence). That run is made here: once per model and
build, at lowest priority, only while the node has nothing queued or running. It may push idle
models off their cards, as any load does, and never wakes a node. Its output is not kept; only
its SHA-256 and a few small frames are, beside the model's evidence.

A failed run marks the build unqualified, which raises an operational alert (`alerts`), and is
not tried again by itself on that build. Run qualification clip, or an image at the largest
configured size, still re-qualifies it.
"""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable

import httpx

from engines import engine_for
from loadcoach.runner import Refusal, Ticket

CHECK_SECONDS = 60  # how often an idle node looks for profiles left stale by a runtime change


class Qualifier:
    """Finds stale image/video profiles and re-qualifies them one at a time while the node is idle."""

    def __init__(self, runner, library, client: httpx.AsyncClient, config, log, folder: Callable[[], Path], idle: Callable[[], bool]):
        """Re-qualify through `runner`, recording runs in `folder()`, only while `idle()` says the node is."""
        self.runner, self.library, self.client, self.config, self.log = runner, library, client, config, log
        self.folder, self.idle = folder, idle
        self.now: str | None = None  # the model being re-qualified, for the catalogue

    # ---- the record of each model's last automatic run ------------------------------------------

    def path(self, name: str) -> Path:
        """Where the last automatic qualification of a model is recorded on this node."""
        return self.folder() / (hashlib.sha256(name.encode()).hexdigest()[:32] + ".json")

    def record(self, name: str) -> dict:
        """The last automatic qualification of a model: its build, outcome, hash and frames."""
        try:
            return json.loads(self.path(name).read_text())
        except (OSError, ValueError):
            return {}

    def keep(self, name: str, record: dict) -> None:
        """Replace a model's record atomically, readable by this account only."""
        path = self.path(name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            json.dump(record, file)
        os.replace(part, path)

    # ---- what the catalogue and the alerts are told ---------------------------------------------

    def state(self, name: str, settings: dict) -> dict:
        """A model's evidence state: pending after a runtime change, failed, carried over, and the last automatic run."""
        evidence, stale = self.runner.image_evidence(name, settings)
        record = self.record(name)
        return {"stale": stale, "pending": stale and bool(self.config.auto_qualify), "running": self.now == name,
                "failed": evidence.get("problem", "") if evidence.get("auto_failed") else "",
                "carried": bool(evidence.get("carried_from")),
                "last": {key: record[key] for key in ("build", "at", "ok", "error", "sha256", "bytes", "frames", "seconds") if key in record} or None}

    def alerts(self) -> list[dict]:
        """One condition per image/video model, open while its automatic qualification failed on the current build."""
        readings = []
        for name in self.library.names():
            settings = self.library.get(name)
            if not settings or engine_for(settings).id != "sdcpp":
                continue
            evidence, _ = self.runner.image_evidence(name, settings)
            failed = bool(evidence.get("auto_failed") and evidence.get("build") == self.runner.known_version(settings))
            node = self.config.machine_name
            readings.append({"key": f"qualification:{node}:{name}", "active": failed,
                             "title": f"Automatic qualification failed: {name} on {node}",
                             "explanation": evidence.get("problem", "") if failed else "",
                             "recovery": "A later run qualified the model, or its runtime or settings changed."})
        return readings

    # ---- finding and running ------------------------------------------------------------------

    def stale(self) -> list[str]:
        """Image and video models still on an earlier build's evidence and not yet tried on this one."""
        found = []
        for name in self.library.names():
            settings = self.library.get(name)
            if not settings or engine_for(settings).id != "sdcpp" or not Path(settings.get("flags", {}).get("model", "")).is_file():
                continue
            if self.runner.image_evidence(name, settings)[1] and self.record(name).get("build") != self.runner.known_version(settings):
                found.append(name)
        return found

    async def requalify(self, name: str) -> dict:
        """Run one qualification of `name` at lowest priority; keep its evidence, or mark this build unqualified."""
        from mirrorwall import qualify
        from mirrorwall.engine import WORKFLOW_CONTRACT
        from mirrorwall.video import is_video
        settings = self.library.get(name)
        video, build, started = is_video(settings), self.runner.known_version(settings), time.time()
        path, body = qualify.request(name, settings)
        record = {"model": name, "build": build, "at": started}
        self.now = name
        try:
            ticket = await self.runner.acquire(Ticket(name, "auto-qualify", "loadcoach", "low", settings=settings))
            try:
                answer = await self.client.post(ticket.server.url + path, headers=ticket.server.headers, json=body,
                                                timeout=httpx.Timeout(None, connect=5.0))
                told = answer.json()
                if answer.status_code != 200:
                    raise ValueError((told.get("error") or {}).get("message") or f"The runtime answered HTTP {answer.status_code}.")
                record.update(await asyncio.to_thread(qualify.sample, told, video))
                await self.runner.measure(ticket.server)  # the evidence this run left, under this build
            finally:
                self.runner.release(ticket)
            if self.runner.image_evidence(name, settings)[1]:
                raise ValueError("The run finished, but its evidence did not qualify the profile on this build.")
            record.update(ok=True, seconds=round(time.time() - started, 1))
        except (Refusal, httpx.HTTPError, ValueError, OSError, KeyError) as problem:
            record.update(ok=False, error=(str(problem) or type(problem).__name__)[:500])
            self.library.measured(name, settings, None, image_profile={"qualified": False, "build": build, "contract": WORKFLOW_CONTRACT,
                                                                        "auto_failed": True, "problem": "Automatic qualification failed: " + record["error"]})
        finally:
            self.now = None
        self.keep(name, record)
        self.log.add("model", "auto-qualified" if record["ok"] else "auto-qualification-failed", "loadcoach", "loadcoach",
                     model=name, build=build, **({} if record["ok"] else {"why": record["error"]}))
        return record

    async def once(self) -> dict | None:
        """Re-qualify the first stale model, if the node is idle and automatic qualification is on."""
        if not self.config.auto_qualify or not self.idle():
            return None
        waiting = self.stale()
        return await self.requalify(waiting[0]) if waiting else None

    async def run(self, gate) -> None:
        """Look for stale profiles every minute for the life of LoadCoach, after database maintenance ends."""
        await gate.wait()
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            try:
                await self.once()
            except Exception as problem:  # noqa: BLE001 - a background check must not end; the next idle pass tries again
                print(json.dumps({"event": "loadcoach.auto_qualify_failed", "why": type(problem).__name__}), flush=True)
