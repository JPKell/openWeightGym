"""Public external-adapter interface; discovery does not import any plugin drivers."""

from plugins._sdk import Adapter, AdapterError, Run

__all__ = ["Adapter", "AdapterError", "Run"]
