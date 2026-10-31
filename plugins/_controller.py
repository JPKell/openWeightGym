"""HTTP synchronization, model discovery and optional external-service health controls."""

from __future__ import annotations

import asyncio
import time

import httpx

from plugins._registry import Registry
from plugins._sdk import AdapterError, MODEL


class Controller:
    """Operate any conforming adapter instance without plugin-specific routing code."""

    def __init__(self, registry: Registry, client: httpx.AsyncClient) -> None:
        """Bind the plugin registry to its managed adapter process controller."""
        self.registry, self.client = registry, client
        self.observation_log = None
        self.observation_polls = {}

    def headers(self, plugin: str, instance: str) -> dict:
        """Use only the dedicated pairing key; openWeightGym account credentials stay on front."""
        return {"authorization": "Bearer " + self.registry.pairing_key(plugin, instance), "X-Suite-Instance": instance}

    async def request(self, plugin: str, instance: str, method: str, path: str, body=None, timeout=35) -> dict:
        """Reach one fixed configured endpoint and translate failures into bounded public errors."""
        config = self.registry.instance(plugin, instance)
        try:
            response = await self.client.request(method, config["url"] + path,
                headers=self.headers(plugin, instance), json=body, timeout=httpx.Timeout(timeout, connect=2))
            if not response.is_success:
                try:
                    message = response.json().get("error", {}).get("message", "Adapter refused the operation.")
                except (ValueError, AttributeError):
                    message = "Adapter refused the operation."
                raise AdapterError(str(message)[:300], response.status_code, "remote_error")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (httpx.HTTPError, OSError, ValueError):
            raise AdapterError("Adapter is unreachable or returned invalid data.", 503, "adapter_unavailable") from None

    async def handshake(self, plugin: str, instance: str) -> dict:
        """Verify plugin identity/protocol before sending any configuration or control action."""
        answer = await self.request(plugin, instance, "GET", "/adapter/v1/capabilities")
        if answer.get("protocol") != 1 or answer.get("plugin") != plugin or not {"send", "receive"} <= set(answer.get("capabilities", [])):
            raise AdapterError("Adapter has a different plugin identity or unsupported protocol.", 409, "protocol_mismatch")
        self.registry.observed(plugin, instance, remote_capabilities=answer["capabilities"])
        return answer

    async def sync(self, plugin: str, instance: str) -> dict:
        """Apply the desired revision and record only what the remote runtime acknowledges."""
        config = self.registry.instance(plugin, instance)
        payload = {"revision": config["revision"], "settings": config["settings"], "policy": {
            "allow_start": config["health"]["allow_start"], "allow_restart": config["health"]["allow_restart"]}}
        try:
            await self.handshake(plugin, instance)
            answer = await self.request(plugin, instance, "PUT", "/adapter/v1/settings", payload)
            if answer.get("revision") != config["revision"] or answer.get("settings") != config["settings"]:
                raise AdapterError("Adapter did not acknowledge the desired settings.", 409, "settings_conflict")
            self.registry.observed(plugin, instance, applied_revision=answer["revision"],
                restart_required=bool(answer.get("restart_required")), synchronized_at=time.time(), error="",
                source_revision=config["revision"], source_url=config["url"])
            return answer
        except AdapterError as problem:
            self.registry.observed(plugin, instance, error=str(problem), source_revision=config["revision"], source_url=config["url"])
            raise

    async def discover(self, plugin: str, instance: str) -> dict:
        """Read declared model discovery and retain public aliases across refreshes."""
        if "models" not in self.registry.manifest(plugin).capabilities:
            raise AdapterError("This plugin does not list models.", 405, "unsupported_capability")
        await self.sync(plugin, instance)
        answer = await self.request(plugin, instance, "GET", "/v1/models")
        rows = answer.get("data")
        if not isinstance(rows, list) or len(rows) > 1000 or not all(
            isinstance(row, dict) and isinstance(row.get("id"), str) and MODEL.fullmatch(row["id"]) for row in rows
        ):
            raise AdapterError("Adapter returned an invalid model inventory.", 502, "invalid_inventory")
        return self.registry.inventory(plugin, instance, rows)

    async def health(self, plugin: str, instance: str, automatic: bool = False) -> dict:
        """Check heartbeat separately from agent state, and optionally perform an allowed recovery."""
        manifest = self.registry.manifest(plugin)
        if "health" not in manifest.capabilities:
            raise AdapterError("This plugin has no health capability.", 405, "unsupported_capability")
        config = self.registry.instance(plugin, instance)
        answer = await self.request(plugin, instance, "GET", "/adapter/v1/health")
        if not isinstance(answer.get("heartbeat"), (int, float)) or not isinstance(answer.get("external"), dict):
            raise AdapterError("Invalid health response.", 502, "invalid_health")
        self.registry.observed(plugin, instance, health_result=answer, health_checked_at=time.time())
        policy, state = config["health"], answer["external"].get("state")
        action = "start" if state == "stopped" and policy["auto_start"] else "restart" if state == "error" and policy["auto_restart"] else None
        if automatic and action and policy["allow_" + action] and action in manifest.capabilities and not answer.get("active_runs"):
            if time.time() - config.get("last_control_at", 0) >= max(60, policy["interval"] * 2):
                await self.control(plugin, instance, action)
        return answer

    async def control(self, plugin: str, instance: str, action: str) -> dict:
        """Send one explicitly allowed service action; never retry it on an ambiguous response."""
        config = self.registry.instance(plugin, instance)
        if action not in ("start", "restart") or action not in self.registry.manifest(plugin).capabilities:
            raise AdapterError("Unsupported service action.", 405, "unsupported_capability")
        if not config["health"]["allow_" + action]:
            raise AdapterError("Allow this action in instance settings first.", 403, "control_disabled")
        if config["revision"] != config.get("applied_revision"):
            await self.sync(plugin, instance)
        self.registry.observed(plugin, instance, last_control_at=time.time())
        return await self.request(plugin, instance, "POST", "/adapter/v1/control", {"action": action}, timeout=65)

    async def tick(self) -> None:
        """Synchronize pending settings and poll enabled health controls independently of WeightRoom."""
        for plugin in self.registry.scan():
            if not plugin["valid"]:
                continue
            for instance, config in plugin["instances"].items():
                try:
                    if config["revision"] != config.get("applied_revision") and time.time() - config.get("sync_attempted_at", 0) >= 10:
                        self.registry.observed(plugin["id"], instance, sync_attempted_at=time.time())
                        await self.sync(plugin["id"], instance)
                    policy = config["health"]
                    if plugin["enabled"] and config["enabled"] and policy["enabled"] and "health" in plugin["capabilities"] and time.time() - config.get("health_checked_at", 0) >= policy["interval"]:
                        await self.health(plugin["id"], instance, automatic=True)
                    key = plugin['id']+':'+instance
                    if self.observation_log and plugin['enabled'] and config['enabled'] and time.monotonic()-self.observation_polls.get(key, 0) >= 10:
                        self.observation_polls[key] = time.monotonic()
                        await self.collect_observations(plugin['id'], instance)
                except AdapterError as problem:
                    self.registry.observed(plugin["id"], instance, error=str(problem), health_checked_at=time.time())

    async def collect_observations(self, plugin, instance):
        """Copy optional adapter spans through the existing controller; acknowledge after commit."""
        from loadcoach.observation_store import receive
        try:
            value = await self.request(plugin, instance, 'GET', '/adapter/v1/observations', timeout=3)
            records = value.get('observations', [])
            await asyncio.to_thread(receive, self.observation_log.db.database, records, 'adapter:'+plugin+':'+instance, value.get('health', {}))
            if records:
                await self.request(plugin, instance, 'POST', '/adapter/v1/observations',
                                   {'ack':[{'id':r['trace_id']+r['id'], 'finished':r.get('finished', True)} for r in records]}, timeout=3)
        except Exception as problem:
            # Optional persistence failures must not stop health/settings supervision.
            from loadcoach.observability import diagnostic
            diagnostic('adapter_observation_unavailable', type(problem).__name__)

    async def run(self) -> None:
        """Keep the controller alive with turnstile; UI restarts do not stop health/settings upkeep."""
        while True:
            await self.tick()
            await asyncio.sleep(2)
