"""Small, standalone plugin contract and strict data-file validation."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from jsonschema import Draft202012Validator

SLUG = re.compile(r"[a-z][a-z0-9_-]{0,47}")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,47}:[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
CAPABILITIES = {"send", "receive", "models", "health", "start", "restart"}


class AdapterError(Exception):
    """A bounded public failure, with an HTTP status and a stable machine-readable code."""

    def __init__(self, message: str, status: int = 502, code: str = "adapter_error") -> None:
        """Initialize the failure message and its status metadata."""
        super().__init__(message)
        self.status, self.code = status, code


def atomic_json(path: Path, value: dict) -> None:
    """Write an owner-only JSON file atomically, including across process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path, default=None):
    """Read persisted state, using a default only when the file does not exist."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def validate(schema: dict, value: dict) -> None:
    """Validate settings and report the field without echoing its potentially private value."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise AdapterError("Settings must be finite JSON values.", 400, "invalid_settings") from None
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: str(list(item.path)))
    if errors:
        error = errors[0]
        field = ".".join(map(str, error.path)) or "settings"
        raise AdapterError(f"{field}: invalid value ({error.validator}).", 400, "invalid_settings")


def local_schema(schema: dict) -> None:
    """Require a self-contained schema, preventing remote reference fetching at validation."""
    Draft202012Validator.check_schema(schema)

    def inspect(value):
        """Validate the supported local JSON-schema structure recursively."""
        if isinstance(value, dict):
            for reference in ("$ref", "$dynamicRef"):
                if reference in value and not str(value[reference]).startswith("#"):
                    raise ValueError("Settings schemas may only use local references.")
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(schema)


@dataclass(frozen=True)
class Manifest:
    """Validated discovery metadata; constructing it does not execute the driver."""

    id: str
    name: str
    version: str
    capabilities: frozenset[str]
    folder: Path
    schema: dict
    driver: Path
    driver_class: str
    files: tuple[Path, ...] = ()

    @classmethod
    def read(cls, folder: Path) -> Manifest:
        """Read bounded manifest/schema files and constrain references to their own folder."""
        resolved = folder.resolve()
        for name in ("manifest.json", "README.md"):
            required = folder / name
            if not required.is_file() or not required.resolve().is_relative_to(resolved):
                raise ValueError("Plugin requires its own manifest.json and README.md.")
        if (folder / "manifest.json").stat().st_size > 64 * 1024:
            raise ValueError("Manifest exceeds 64 KiB.")
        document = read_json(folder / "manifest.json")
        expected = {"protocol", "id", "name", "version", "capabilities", "schema", "driver", "class"}
        if not isinstance(document, dict) or not expected <= set(document) or set(document) - expected - {"files"}:
            raise ValueError("Manifest fields must be: " + ", ".join(sorted(expected)))
        if document["protocol"] != 1 or not SLUG.fullmatch(document["id"]) or document["id"] != folder.name:
            raise ValueError("Protocol must be 1 and plugin ID must match its folder slug.")
        capabilities = document["capabilities"]
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise ValueError("Capabilities must be a list of names.")
        if set(capabilities) - CAPABILITIES or not {"send", "receive"} <= set(capabilities):
            raise ValueError("A model plugin needs send/receive and only supported capabilities.")
        if not all(isinstance(document[key], str) and 0 < len(document[key]) <= 200 for key in ("name", "version", "class")):
            raise ValueError("Name, version and driver class must be bounded strings.")
        if not document["class"].isidentifier():
            raise ValueError("Driver class must be a Python identifier.")
        resolved = folder.resolve()
        files = []
        for name, suffix in (("schema", ".json"), ("driver", ".py")):
            if not isinstance(document[name], str):
                raise ValueError("Driver and schema must name relative files.")
            relative = Path(document[name])
            target = (folder / relative).resolve()
            if relative.is_absolute() or not target.is_relative_to(resolved) or target.suffix != suffix or not target.is_file():
                raise ValueError("Driver/schema references must stay inside the plugin folder.")
            files.append(target)
        if files[0].stat().st_size > 256 * 1024:
            raise ValueError("Settings schema exceeds 256 KiB.")
        schema = read_json(files[0])
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Settings schema must describe an object.")
        local_schema(schema)
        resources = []
        for name in document.get("files", []):
            target = (folder / name).resolve()
            if Path(name).is_absolute() or not target.is_relative_to(resolved) or not target.is_file() or any(
                part.startswith(".") or part in ("data", "keys") for part in Path(name).parts
            ) or target.name == "settings.json" or target.suffix == ".key":
                raise ValueError("Additional files must be explicit plugin resources, never state or keys.")
            resources.append(target)
        return cls(document["id"], document["name"], document["version"], frozenset(capabilities),
                   resolved, schema, files[1], document["class"], tuple(resources))

    def load(self) -> Adapter:
        """Explicitly execute only the driver selected by an installed adapter server."""
        package = "suite_adapter_" + self.id
        namespace = types.ModuleType(package)
        namespace.__path__ = [str(self.driver.parent)]
        sys.modules[package] = namespace
        spec = importlib.util.spec_from_file_location(package + ".driver", self.driver)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        driver = getattr(module, self.driver_class)()
        if not isinstance(driver, Adapter):
            raise ValueError("Plugin driver must implement plugins._sdk.Adapter.")
        methods = {"send": "events", "receive": "events", "models": "models", "health": "health", "start": "control", "restart": "control"}
        if any(getattr(type(driver), methods[capability]) is getattr(Adapter, methods[capability]) for capability in self.capabilities):
            raise ValueError("Plugin driver must implement every declared capability.")
        return driver


@dataclass(frozen=True)
class Run:
    """One immutable run snapshot; the caller cannot replace approval or machine settings."""

    request_id: str
    owner: str
    profile: str
    messages: list[dict]
    persistent: bool
    native_session: str | None
    session_title: str
    reasoning: str | None = None


class Adapter:
    """Interface implemented inside each plugin folder; events must end with a result."""

    async def models(self, settings: dict) -> list[dict]:
        """List external profiles/models as id/name objects, without selecting a default."""
        raise NotImplementedError

    async def health(self, settings: dict) -> dict:
        """Report external-service state independently from HTTP runtime reachability."""
        raise AdapterError("Health is not supported.", 405, "unsupported_capability")

    async def control(self, action: str, settings: dict) -> dict:
        """Perform one explicitly allowed external-service start/restart operation."""
        raise AdapterError("Control is not supported.", 405, "unsupported_capability")

    async def events(self, run: Run, settings: dict) -> AsyncIterator[dict]:
        """Yield normalized session, preview, reasoning, tool, result and error events."""
        raise NotImplementedError
        yield {}  # Keeps the base method an async generator for the streaming interface.
