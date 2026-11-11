"""Discover the devices the configured llama-server can actually load models onto."""

import os
import re
import subprocess

from loadcoach.gpu_type import is_integrated


def detect(llama_server: str, *, include_integrated=False) -> list[dict]:
    """Read device names and descriptions without starting a model; fail closed on errors."""
    try:
        result = subprocess.run([llama_server, "--list-devices"], capture_output=True, text=True, timeout=15, env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    found = [{"id": match[1], "description": match[2]} for line in (result.stdout or "").splitlines()
             if (match := re.match(r"\s+([A-Za-z]+\d+):\s+(.+)", line))]
    return [device for device in found if include_integrated or not is_integrated(device)]
