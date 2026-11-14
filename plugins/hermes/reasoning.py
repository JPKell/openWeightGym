"""Hermes reasoning choices from the shipped CLI settings contract."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def reasoning_levels() -> tuple[str, ...] | None:
    """Return declared CLI levels; None means unavailable, () means none declared.

    Read data only, so control node can render choices without importing the CLI driver.
    The immutable installed schema is shared by global and per-profile settings.
    """
    try:
        schema = json.loads(Path(__file__).with_name("schema.json").read_text())
        levels = schema["properties"]["reasoning"]["enum"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(levels, list) or not all(isinstance(level, str) for level in levels):
        return None
    return tuple(level for level in levels if level)
