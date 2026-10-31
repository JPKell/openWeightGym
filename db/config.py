"""Validation and lookup for named database stores and service bindings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .database import Database, DatabaseConfigurationError, DatabaseSource, Store, database

NAME = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
STORE_FIELDS = {"engine", "path", "dsn_file", "display_name", "options"}


@dataclass(frozen=True)
class BoundStore:
    """One named store together with every repository bound to it."""

    name: str
    database: Database
    bindings: tuple[str, ...]


class DatabaseRegistry:
    """Resolve service/repository binding names to reusable Database instances."""

    def __init__(self, stores: Mapping[str, Database], bindings: Mapping[str, str]) -> None:
        """Resolve named stores and service bindings relative to the installation."""
        self._stores = dict(stores)
        self._bindings = dict(bindings)
        for binding, store in self._bindings.items():
            if not NAME.fullmatch(binding):
                raise DatabaseConfigurationError(f"invalid database binding name {binding!r}")
            if store not in self._stores:
                raise DatabaseConfigurationError(f"database binding {binding!r} names unknown store {store!r}")

    @classmethod
    def from_config(
        cls,
        configured: dict | None,
        base: str | Path,
        defaults: Mapping[str, DatabaseSource] | None = None,
    ) -> DatabaseRegistry:
        """Build named stores over compatibility defaults, resolving secret paths locally."""
        base = Path(base)
        section = configured or {}
        if not isinstance(section, dict) or set(section) - {"stores", "bindings"}:
            raise DatabaseConfigurationError("[databases] may contain stores and bindings only")
        raw_stores, raw_bindings = section.get("stores", {}), section.get("bindings", {})
        if not isinstance(raw_stores, dict) or not isinstance(raw_bindings, dict):
            raise DatabaseConfigurationError("database stores and bindings must be tables")

        stores: dict[str, Database] = {}
        bindings: dict[str, str] = {}
        for binding, source in (defaults or {}).items():
            if not NAME.fullmatch(binding):
                raise DatabaseConfigurationError(f"invalid default database binding name {binding!r}")
            store_name = "default_" + re.sub(r"[^a-z0-9]+", "_", binding.lower()).strip("_")
            candidate, suffix = store_name, 2
            while candidate in stores:
                candidate, suffix = f"{store_name}_{suffix}", suffix + 1
            stores[candidate] = database(source)
            bindings[binding] = candidate

        for name, values in raw_stores.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise DatabaseConfigurationError(f"invalid database store name {name!r}")
            if not isinstance(values, dict) or set(values) - STORE_FIELDS:
                raise DatabaseConfigurationError(
                    f"store {name!r} may set {', '.join(sorted(STORE_FIELDS))} only"
                )
            engine = values.get("engine")
            if not isinstance(engine, str):
                raise DatabaseConfigurationError(f"store {name!r}: engine is required")
            path = values.get("path", "")
            dsn_file = values.get("dsn_file", "")
            if path and (not isinstance(path, str) or "://" in path):
                raise DatabaseConfigurationError(f"store {name!r}: SQLite path must be a local path")
            if dsn_file and not isinstance(dsn_file, str):
                raise DatabaseConfigurationError(f"store {name!r}: dsn_file must be a path")
            if path and path != ":memory:" and not path.startswith("file:"):
                path = str((base / path).resolve()) if not Path(path).is_absolute() else path
            if dsn_file:
                dsn_file = str((base / dsn_file).resolve()) if not Path(dsn_file).is_absolute() else dsn_file
            display_name = values.get("display_name", "")
            if not isinstance(display_name, str):
                raise DatabaseConfigurationError(f"store {name!r}: display_name must be text")
            options = values.get("options", {})
            if isinstance(options, dict) and any(
                word in str(key).lower() for key in options for word in ("password", "secret", "token", "dsn", "conninfo")
            ):
                raise DatabaseConfigurationError(
                    f"store {name!r}: credentials belong in dsn_file, not options"
                )
            stores[name] = Database(Store(name, engine, path, dsn_file, display_name, options))

        for binding, store in raw_bindings.items():
            if not isinstance(binding, str) or not isinstance(store, str):
                raise DatabaseConfigurationError("database bindings must map names to store names")
            bindings[binding] = store
        return cls(stores, bindings)

    def database(self, binding: str) -> Database:
        """Return the configured database for one logical owner."""
        try:
            return self._stores[self._bindings[binding]]
        except KeyError:
            raise DatabaseConfigurationError(f"no database is bound for {binding!r}") from None

    def connect(self, binding: str, **options):
        """Open a connection for one logical owner."""
        return self.database(binding).connect(**options)

    def stores(self) -> list[dict]:
        """Describe configured stores without exposing DSNs or secret file contents."""
        owners: dict[str, list[str]] = {name: [] for name in self._stores}
        for binding, name in self._bindings.items():
            owners[name].append(binding)
        return [
            {
                "name": name,
                "engine": target.engine,
                "display_name": target.display_name,
                "path": str(target.path) if target.path else "",
                "bindings": sorted(owners[name]),
            }
            for name, target in sorted(self._stores.items())
        ]

    def bound_stores(self) -> list[BoundStore]:
        """Return database objects for trusted maintenance code without exposing credentials."""
        owners: dict[str, list[str]] = {name: [] for name in self._stores}
        for binding, name in self._bindings.items():
            owners[name].append(binding)
        return [
            BoundStore(name, target, tuple(sorted(owners[name])))
            for name, target in sorted(self._stores.items())
        ]
