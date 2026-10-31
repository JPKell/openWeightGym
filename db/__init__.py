"""Engine-neutral database access and named store bindings for openWeightGym services."""

from .config import BoundStore, DatabaseRegistry
from .database import (
    Database,
    DatabaseConfigurationError,
    DatabaseSource,
    DriverUnavailable,
    Store,
    adopt,
    connect,
    database,
    is_memory,
    local_path,
)
from .migrations import Migration, add_column, columns, run as migrate, version as schema_version

__all__ = [
    "Database",
    "DatabaseConfigurationError",
    "DatabaseRegistry",
    "BoundStore",
    "DatabaseSource",
    "DriverUnavailable",
    "Store",
    "adopt",
    "connect",
    "database",
    "is_memory",
    "local_path",
    "Migration",
    "migrate",
    "columns",
    "add_column",
    "schema_version",
]
