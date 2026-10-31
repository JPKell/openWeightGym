"""Discover the devices the configured llama-server can actually load models onto."""

import os
import re
import subprocess


def detect(llama_server: str) -> list[dict]:
    """Read device names and descriptions without starting a model; fail closed on errors."""
    try:
        result = subprocess.run([llama_server, "--list-devices"], capture_output=True, text=True, timeout=15, env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    return [{"id": match[1], "description": match[2]} for line in (result.stdout or "").splitlines()
            if (match := re.match(r"\s+([A-Za-z]+\d+):\s+(.+)", line))]
