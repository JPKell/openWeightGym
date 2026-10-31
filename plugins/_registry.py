"""Front-owned manifests, instance settings, public aliases and minimal adapter-use records."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from db import DatabaseSource, database as database_target, local_path, migrate
from db.schemas import PLUGINS

from plugins._auth import Authorization
from plugins._sdk import ALIAS, SLUG, MODEL, AdapterError, Manifest, atomic_json, read_json, validate

LEVELS = ("normal", "sensitive", "restricted")


class Registry:
    """Read discovery without importing code; coordinate front/turnstile state through private files."""

    def __init__(self, root: Path, data: Path, database: DatabaseSource | None = None) -> None:
        """Open plugin configuration and activity storage with portable schema migrations."""
        self.root, self.data = root.resolve(), data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.auth = Authorization(self.data / "plugin-auth.json")
        database = database_target(database or self.data / "plugins.db")
        self.db = database.connect(check_same_thread=False)
        if file := local_path(database):
            os.chmod(file, 0o600)
        migrate(database, self.db, "weightroom.plugins", PLUGINS)

    def scan(self) -> list[dict]:
        """List valid manifests and actionable errors; disabled drivers are never executed."""
        result = []
        for folder in sorted(self.root.glob("*/")):
            if not (folder / "manifest.json").exists():
                continue
            try:
                if not folder.resolve().is_relative_to(self.root):
                    raise ValueError("Plugin folder must stay inside the plugin root.")
                manifest = Manifest.read(folder)
                state = self.state(manifest.id)
                result.append({"id": manifest.id, "name": manifest.name, "version": manifest.version,
                    "capabilities": sorted(manifest.capabilities), "valid": True, "enabled": state["enabled"],
                    "instances": state["instances"], "schema": manifest.schema})
            except Exception as problem:
                result.append({"id": folder.name, "name": folder.name, "valid": False, "enabled": False,
                               "error": str(problem)[:300], "instances": {}})
        return result

    def manifest(self, plugin: str) -> Manifest:
        """Resolve a bounded plugin ID without allowing paths through management endpoints."""
        if not isinstance(plugin, str) or not SLUG.fullmatch(plugin):
            raise AdapterError("Unknown plugin.", 404, "plugin_not_found")
        folder = self.root / plugin
        if not folder.resolve().is_relative_to(self.root):
            raise AdapterError("Unknown plugin.", 404, "plugin_not_found")
        try:
            return Manifest.read(folder)
        except (OSError, ValueError, TypeError):
            raise AdapterError("Plugin manifest is invalid or unavailable.", 400, "invalid_plugin") from None

    def state(self, plugin: str) -> dict:
        """Read the atomically replaced plugin-local settings file, with discovery-only defaults."""
        try:
            state = read_json(self.root / plugin / "settings.json", {"enabled": False, "instances": {}})
        except (OSError, ValueError):
            raise AdapterError("Invalid plugin settings file.", 400, "invalid_settings") from None
        if not isinstance(state, dict) or type(state.get("enabled")) is not bool or not isinstance(state.get("instances"), dict):
            raise AdapterError("Invalid plugin settings file.", 400, "invalid_settings")
        for name, config in state["instances"].items():
            if not SLUG.fullmatch(name) or not isinstance(config, dict) or any(
                type(config.get(key)) is not int or config[key] < 0 for key in ("revision", "applied_revision")
            ) or not isinstance(config.get("url"), str) or not isinstance(config.get("settings"), dict) or type(config.get("enabled")) is not bool or not isinstance(config.get("models"), dict) or not isinstance(config.get("health"), dict):
                raise AdapterError("Invalid instance settings file.", 400, "invalid_settings")
            if set(config["health"]) != {"enabled", "interval", "allow_start", "allow_restart", "auto_start", "auto_restart"}:
                raise AdapterError("Invalid health settings file.", 400, "invalid_settings")
            if type(config["health"]["interval"]) is not int or not 5 <= config["health"]["interval"] <= 86400 or any(type(value) is not bool for key, value in config["health"].items() if key != "interval") or config.get("max_sensitivity") not in LEVELS:
                raise AdapterError("Invalid health/sensitivity settings file.", 400, "invalid_settings")
            for profile, model in config["models"].items():
                if not MODEL.fullmatch(profile) or not isinstance(model, dict) or not isinstance(model.get("alias"), str) or not ALIAS.fullmatch(model["alias"]) or not model["alias"].startswith(plugin + ":") or type(model.get("enabled")) is not bool:
                    raise AdapterError("Invalid model settings file.", 400, "invalid_settings")
        return state

    def change(self, plugin: str, mutate) -> dict:
        """Serialize read/modify/write updates across WeightRoom and turnstile processes."""
        manifest = self.manifest(plugin)
        lock_file = manifest.folder / ".settings.lock"
        descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            state = self.state(plugin)
            mutate(state)
            atomic_json(manifest.folder / "settings.json", state)
            return state
        finally:
            os.close(descriptor)

    def enable(self, plugin: str, enabled: bool) -> dict:
        """Enable discovery/routing without starting or stopping an external service."""
        if type(enabled) is not bool:
            raise AdapterError("enabled must be boolean.", 400, "invalid_settings")
        return self.change(plugin, lambda state: state.update(enabled=enabled))

    def key_path(self, plugin: str, instance: str) -> Path:
        """Keep each instance's pairing key outside its normal settings and source files."""
        return self.data.parent / "keys/plugins" / plugin / (instance + ".key")

    def pairing_key(self, plugin: str, instance: str) -> str:
        """Read an existing instance key, without returning it in ordinary management responses."""
        return self.key_path(plugin, instance).read_text().strip()

    def save_instance(self, plugin: str, instance: str, url: str, settings: dict, *, enabled=False,
                      health: dict | None = None, sensitivity: str = "normal") -> dict:
        """Validate locally, create a pairing key once, and mark a new desired revision pending."""
        manifest = self.manifest(plugin)
        if not isinstance(instance, str) or not SLUG.fullmatch(instance):
            raise AdapterError("Instance must be a lowercase slug.", 400, "invalid_instance")
        try:
            parsed = urlsplit(url)
            parsed.port
        except (TypeError, ValueError):
            raise AdapterError("Invalid adapter URL or port.", 400, "invalid_url") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or len(url) > 2000:
            raise AdapterError("Use an HTTP(S) URL without credentials, query or fragment.", 400, "invalid_url")
        if type(enabled) is not bool or sensitivity not in LEVELS:
            raise AdapterError("Invalid enablement or sensitivity.", 400, "invalid_settings")
        validate(manifest.schema, settings)
        policy = {"enabled": False, "interval": 30, "allow_start": False, "allow_restart": False,
                  "auto_start": False, "auto_restart": False, **(health or {})}
        if set(policy) != {"enabled", "interval", "allow_start", "allow_restart", "auto_start", "auto_restart"} or any(
            type(value) is not bool for key, value in policy.items() if key != "interval"
        ) or type(policy["interval"]) is not int or not 5 <= policy["interval"] <= 86400:
            raise AdapterError("Invalid health policy.", 400, "invalid_settings")
        for action in ("start", "restart"):
            if (policy["allow_" + action] or policy["auto_" + action]) and action not in manifest.capabilities:
                raise AdapterError("Plugin does not support " + action + ".", 400, "unsupported_capability")
            if policy["auto_" + action] and not policy["allow_" + action]:
                raise AdapterError("Allow " + action + " before enabling automatic control.", 400, "invalid_settings")
        key = self.key_path(plugin, instance)
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(key.parent, 0o700)
        try:
            descriptor = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(secrets.token_urlsafe(48) + "\n")
        except FileExistsError:
            pass

        def save(state):
            """Persist the validated plugin instance in the current registry transaction."""
            previous = state["instances"].get(instance, {})
            changed = settings != previous.get("settings") or any(policy[key] != previous.get("health", {}).get(key) for key in ("allow_start", "allow_restart"))
            revision = max(previous.get("revision", 0), previous.get("applied_revision", 0)) + int(changed)
            state["instances"][instance] = {**previous, "url": url.rstrip("/"), "settings": settings, "enabled": enabled,
                "health": policy, "max_sensitivity": sensitivity, "revision": revision or 1,
                "applied_revision": 0 if url.rstrip("/") != previous.get("url") else previous.get("applied_revision", 0),
                "models": previous.get("models", {}), "error": "", "updated_at": time.time()}

        return self.change(plugin, save)["instances"][instance]

    def instance(self, plugin: str, instance: str) -> dict:
        """Resolve only a configured instance and reject any path-like identifier."""
        self.manifest(plugin)
        if not isinstance(instance, str) or not SLUG.fullmatch(instance):
            raise AdapterError("Unknown instance.", 404, "instance_not_found")
        found = self.state(plugin)["instances"].get(instance)
        if not found:
            raise AdapterError("Unknown instance.", 404, "instance_not_found")
        return found

    def observed(self, plugin: str, instance: str, *, source_revision=None, source_url=None, **fields) -> None:
        """Merge controller observations without overwriting a concurrent settings edit."""
        def record(state):
            """Persist the adapter observation and its public availability metadata."""
            if instance in state["instances"]:
                current = state["instances"][instance]
                if source_url is not None and current["url"] != source_url:
                    return
                changes = fields if source_revision is None or current["revision"] == source_revision else {
                    key: value for key, value in fields.items() if key == "applied_revision"}
                if "applied_revision" in changes:
                    changes = {**changes, "applied_revision": max(changes["applied_revision"], current["applied_revision"])}
                current.update(changes)
        self.change(plugin, record)

    def aliases(self) -> dict[str, tuple[str, str, str]]:
        """Reserve names across all instances, including disabled entries, to avoid collisions."""
        found = {}
        for plugin in self.scan():
            if plugin["valid"]:
                for instance, config in plugin["instances"].items():
                    for profile, model in config.get("models", {}).items():
                        alias = model["alias"]
                        if alias in found:
                            raise AdapterError("Duplicate public model alias.", 409, "alias_conflict")
                        found[alias] = (plugin["id"], instance, profile)
        return found

    def inventory(self, plugin: str, instance: str, rows: list[dict]) -> dict:
        """Refresh native entries, preserving selections and aliases; new entries start disabled."""
        def update(state):
            """Replace observed inventory while preserving operator-owned configuration."""
            reserved = self.aliases()
            config = state["instances"][instance]
            previous = config.get("models", {})
            models = {name: {**value, "available": False} for name, value in previous.items()}
            for row in rows:
                name = row["id"]
                tail = re.sub(r"[^A-Za-z0-9._/-]", "-", name)
                if len(tail) > 100:
                    tail = tail[:80] + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
                default = plugin + ":" + tail
                if default in reserved and reserved[default] != (plugin, instance, name):
                    default = plugin + ":" + instance + "/" + tail[:60]
                if default in reserved and reserved[default] != (plugin, instance, name):
                    default = default[:default.index(":") + 110] + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
                reserved[default] = (plugin, instance, name)
                models[name] = {**previous.get(name, {"alias": default, "enabled": False}),
                                "label": str(row.get("name") or name)[:200], "available": True}
            config.update(models=models, discovered_at=time.time())
        return self.change(plugin, update)["instances"][instance]

    def model(self, plugin: str, instance: str, profile: str, alias: str, enabled: bool) -> None:
        """Rename/select a public alias without changing its underlying native profile."""
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or not alias.startswith(plugin + ":") or type(enabled) is not bool:
            raise AdapterError("Use a unique public name beginning with " + plugin + ":", 400, "invalid_alias")
        def update(state):
            """Persist the selected model's validated operator configuration."""
            current = self.aliases().get(alias)
            if current and current != (plugin, instance, profile):
                raise AdapterError("That public name is already assigned.", 409, "alias_conflict")
            models = state["instances"][instance]["models"]
            if profile not in models:
                raise AdapterError("Unknown discovered profile.", 404, "model_not_found")
            models[profile].update(alias=alias, enabled=enabled)
        self.change(plugin, update)

    def add_model(self, plugin: str, instance: str, profile: str) -> None:
        """Allow explicit underlying entries for plugins that do not declare model discovery."""
        if not isinstance(profile, str) or not MODEL.fullmatch(profile):
            raise AdapterError("Invalid underlying model/profile ID.", 400, "invalid_model")
        previous = self.instance(plugin, instance).get("models", {})
        rows = [{"id": name, "name": item.get("label", name)} for name, item in previous.items()]
        rows.append({"id": profile, "name": profile})
        self.inventory(plugin, instance, rows)

    def resolve(self, alias: str) -> tuple[str, str, str, dict] | None:
        """Route only enabled plugin/instance/profile combinations, using current file state."""
        if not isinstance(alias, str):
            return None
        for plugin in self.scan():
            if not plugin["valid"] or not plugin["enabled"]:
                continue
            for instance, config in plugin["instances"].items():
                if config.get("enabled"):
                    for profile, model in config.get("models", {}).items():
                        if model["alias"] == alias and model.get("enabled") and model.get("available"):
                            return plugin["id"], instance, profile, config
        return None

    def owns_prefix(self, alias: str) -> bool:
        """Keep unknown/disabled adapter names from falling through to compute and waking it."""
        return isinstance(alias, str) and alias.partition(":")[0] in {row["id"] for row in self.scan()}

    def models(self, user: dict) -> list[dict]:
        """Offer adapter models under the caller's effective permission and allowlist."""
        result = []
        for plugin in self.scan():
            if plugin["valid"] and plugin["enabled"]:
                for instance, config in plugin["instances"].items():
                    if config.get("enabled"):
                        for profile, model in config.get("models", {}).items():
                            alias = model["alias"]
                            if model.get("enabled") and model.get("available") and self.auth.permits(user, alias):
                                result.append({"id": alias, "object": "model", "created": 0, "owned_by": "adaptor",
                                    "adaptor": {"plugin": plugin["id"], "instance": instance, "profile": profile},
                                    "capabilities": {"chat": True, "stream": True, "agent_tools": True, "caller_tools": False},
                                    "available": config.get("revision") == config.get("applied_revision") and not config.get("error")})
        return result

    def enrich(self, user: dict) -> dict:
        """Add adapter metadata to the account view consumed by openWeightGym chat, without GPU flags."""
        adapters = self.models(user)
        options = {row["id"]: {"adaptor": True, "context": 0, "vision": True, "hosted": False} for row in adapters}
        return {**user, "models": list(dict.fromkeys([*user.get("models", []), *options])),
                "chat": {**user.get("chat", {}), "models": {**user.get("chat", {}).get("models", {}), **options}}}

    def record(self, user: dict, model: str, plugin: str, instance: str, profile: str,
               outcome: str, session: str | None = None, request: str | None = None) -> None:
        """Append minimal adapter-use metadata; never accept prompt, answer or usage fields."""
        with self.db:
            self.db.execute("INSERT INTO uses (at, owner, user, model, plugin, instance, profile, session, request, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), user["identity"], user["name"], model, plugin, instance, profile, session, request, outcome))

    def activity(self, user: dict, limit: int = 100, offset: int = 0) -> list[dict]:
        """Show one's own uses, or everyone's when the effective account has see-log."""
        all_uses = "see-log" in user.get("can", [])
        query = "SELECT * FROM uses" + ("" if all_uses else " WHERE owner = ?") + " ORDER BY id DESC LIMIT ? OFFSET ?"
        parameters = (limit, offset) if all_uses else (user["identity"], limit, offset)
        names = ("id", "at", "owner", "user", "model", "plugin", "instance", "profile", "session", "request", "outcome")
        return [dict(zip(names, row)) for row in self.db.execute(query, parameters)]

    def activity_count(self, user: dict) -> int:
        """Count plugin requests still active for the selected instance."""
        all_uses = "see-log" in user.get("can", [])
        return self.db.execute("SELECT COUNT(*) FROM uses" + ("" if all_uses else " WHERE owner = ?"),
                               () if all_uses else (user['identity'],)).fetchone()[0]
