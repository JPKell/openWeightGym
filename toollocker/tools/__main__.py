"""The tool host: serve every tool in a tool folder, over stdio or HTTP.

    python -m toollocker.tools FOLDER [--settings FILE] [--http PORT --key-file FILE]

FOLDER holds the tool files. FILE is a TOML file with a section for each tool file that needs
settings, such as `[files] folders = [...]`. Without --http, it speaks stdio, so a ToolLocker on
the same machine can run it as a server's `command`. A tool file added or changed counts from
the next start. See toollocker/README.md.
"""

import argparse
import tomllib
from pathlib import Path

from toollocker.tools import load, serve

parser = argparse.ArgumentParser(prog="python -m toollocker.tools", description="Serve every tool in a tool folder.")
parser.add_argument("folder", type=Path, help="the folder of tool files")
parser.add_argument("--settings", type=Path, metavar="FILE", help="a TOML file with a [NAME] section for each tool file NAME.py that needs one")
parser.add_argument("--http", type=int, metavar="PORT", help="answer over HTTP on this port, for a ToolLocker on another machine")
parser.add_argument("--bind", default="", metavar="ADDRESS", help="interface address for HTTP (default: all interfaces)")
parser.add_argument("--key-file", metavar="FILE", help="the file holding the key that ToolLocker shows; needed with --http")
told = parser.parse_args()
if (told.http is None) != (told.key_file is None):
    parser.error("--http and --key-file go together")
if not told.folder.is_dir():
    parser.error(f"{told.folder} is not a folder")
try:
    settings = tomllib.loads(told.settings.read_text()) if told.settings else {}
except (OSError, tomllib.TOMLDecodeError) as problem:
    parser.error(f"the settings could not be read: {problem}")
tools = load(told.folder, settings)
if not tools:
    raise SystemExit(f"tools: no tools to serve in {told.folder}")
serve("tools", *tools, http=(told.http, told.key_file, told.bind) if told.http is not None else None)
