"""A tool server for the ToolLocker's tests: tools that answer, wait, refuse and die.

The first argument, if given, becomes `echo`'s description, so a test can change it between runs.
"""

import os
import sys
import time

from toollocker.tools import serve


def echo(text: str, times: int = 1) -> str:
    """Say it back."""
    return text * int(times)


def nap(seconds: int) -> str:
    """Wait, then say so."""
    time.sleep(int(seconds))
    return "rested"


def refuse() -> str:
    """Always says no."""
    raise ValueError("No.")


def pid() -> str:
    """Which process this is."""
    return str(os.getpid())


def die() -> str:
    """Ends the server without answering."""
    os._exit(1)


if len(sys.argv) > 1:
    echo.__doc__ = sys.argv[1]
serve("fake", echo, nap, refuse, pid, die)
