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


def held(folder: Path = FOLDER) -> dict[int, dict[int, int]]:
    """What every running process uses of each card: {pid: {card: MiB}}. Pid 0 is everything that is not a model server."""
    return {
        int(file.stem): {int(card): mib for card, mib in json.loads(file.read_text()).items()}
        for file in folder.glob("*.json")
        if file.stem == "0" or alive(int(file.stem))
    }


def answer(arguments: list[str], folder: Path | None = None) -> str:
    """What this stand-in prints for its arguments. The tests also ask it in their own process (see conftest.py)."""
    folder = Path(os.environ.get("FAKE_CARDS", ".")) if folder is None else folder
    if '--query-gpu=index,uuid,name,memory.total,driver_version' in arguments:
        # Probe identity must not accidentally include transient memory usage as a driver version.
        lines = [f'{card}, GPU-{card}, Fake GPU {card}, {TOTAL}, fake-driver-1' for card in (0, 1)]
    elif any(argument.startswith("--query-gpu") for argument in arguments):  # index, uuid, memory.total, memory.used
        uses = held(folder)
        lines = [f"{card}, GPU-{card}, {TOTAL}, {sum(one.get(card, 0) for one in uses.values())}" for card in (0, 1)]
    else:  # --query-compute-apps: pid, gpu_uuid, used_memory
        lines = [f"{pid}, GPU-{card}, {mib}" for pid, uses in held(folder).items() for card, mib in uses.items() if pid]
    return "".join(line + "\n" for line in lines)


if __name__ == "__main__":
    sys.stdout.write(answer(sys.argv[1:]))
