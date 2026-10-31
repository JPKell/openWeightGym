"""Attach to the prepared child cgroup before replacing this process with an engine."""
import argparse
import os
from pathlib import Path
import sys

# Executed by absolute path with an isolated interpreter, not PYTHONPATH or a shell.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loadcoach.engine_memory import enter


def main():
    """Join the delegated memory cgroup before executing the owned engine command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup", required=True)
    parser.add_argument("--memory-mib", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not Path(command[0]).is_absolute():
        parser.error("Engine command must name an absolute executable.")
    enter(args.cgroup, args.memory_mib)
    os.execve(command[0], command, dict(os.environ))


if __name__ == "__main__":
    main()
