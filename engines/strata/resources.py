"""Strata's explicit hybrid reservation; never borrow llama.cpp's spill policy."""
from engines.strata.config import NodeSettings


def host_ram_budget(config, settings: dict) -> int:
    """Return the engine host-memory reservation in MiB."""
    node = NodeSettings.read(config.engines.get("strata", {}))
    if not node.enabled:
        raise ValueError("Strata is disabled on this node.")
    # Reserve the entire configured cap, not a steady-state RSS estimate. Native cgroup
    # enforcement and artifact/build qualification remain separate launch requirements.
    return node.host_ram_mib
