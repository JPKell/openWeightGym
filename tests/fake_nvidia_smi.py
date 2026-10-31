"""Stands in for nvidia-smi in tests: two made-up cards of 16000 MiB, and the two questions LoadCoach asks of them.

What a process uses is a file in the folder $FAKE_CARDS: `PID.json` holds {"card number": MiB}.
The stand-in for llama-server writes one for itself when it is started with `--mib`. A file
`0.json` is for everything that is not a model server, such as a desktop. A process that has
ended counts for nothing, as with the real cards.
"""

import json
import os
import sys
from pathlib import Path

FOLDER = Path(os.environ.get("FAKE_CARDS", "."))
TOTAL = 16000  # MiB on each card


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def held() -> dict[int, dict[int, int]]:
    """What every running process uses of each card: {pid: {card: MiB}}. Pid 0 is everything that is not a model server."""
    return {
        int(file.stem): {int(card): mib for card, mib in json.loads(file.read_text()).items()}
        for file in FOLDER.glob("*.json")
        if file.stem == "0" or alive(int(file.stem))
    }


if __name__ == "__main__":
    if '--query-gpu=index,uuid,name,memory.total,driver_version' in sys.argv:
        # Probe identity must not accidentally include transient memory usage as a driver version.
        for card in (0, 1):
            print(f'{card}, GPU-{card}, Fake GPU {card}, {TOTAL}, fake-driver-1')
    elif any(argument.startswith("--query-gpu") for argument in sys.argv):  # index, uuid, memory.total, memory.used
        for card in (0, 1):
            print(f"{card}, GPU-{card}, {TOTAL}, {sum(uses.get(card, 0) for uses in held().values())}")
    else:  # --query-compute-apps: pid, gpu_uuid, used_memory
        for pid, uses in held().items():
            for card, mib in uses.items():
                if pid:
                    print(f"{pid}, GPU-{card}, {mib}")
