"""What every tool server shares: speaking MCP, and finding the tools in a tool folder.

A tool server is a small program. The ToolLocker starts it, writes one JSON message per line to
its input and reads one per line from its output. That is all of MCP's "stdio" transport.

Given `--http PORT --key-file FILE`, the same program answers over HTTP instead, so it can run
on another machine with a ToolLocker reaching it by address. Only a caller with the key is answered.

A tool is an ordinary function marked `@tool`. Its name, its docstring and its parameters are
what a model is shown, so the docstring is written for the model. A tool returns text. To
refuse, it raises ValueError with the reason.

A tool folder is a folder of such files, each with one tool or several. `python -m
toollocker.tools FOLDER` serves every tool in it; see __main__.py and toollocker/README.md.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import secrets
import sys
import traceback
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# How a parameter's Python type is named in the JSON Schema a model is shown.
TYPES = {str: "string", int: "integer", bool: "boolean"}

LONGEST = 40_000  # characters of result handed back; more than this floods a model's context


def tool(function: Callable) -> Callable:
    """Mark a function as a tool, so that a tool folder offers it. The function itself is unchanged."""
    function.is_tool = True
    return function


def load(folder: Path, settings: dict) -> list[Callable]:
    """Every tool in the files of a tool folder, in file and then definition order.

    Each `*.py` file not starting with `_` is read. A file with a `setup(settings)` function is
    handed its own section of the settings, `[NAME]` for NAME.py, and is left out if there is
    none. A file that cannot be read or set up, or whose tool takes a parameter of a kind a model
    cannot be shown, is left out and the reason printed; the rest are served. Two tools of one
    name in two files stop it, since a harness could not tell which it called.
    """
    found: dict[str, str] = {}  # tool name -> the file it is in
    tools: list[Callable] = []
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"tool_folder.{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            if hasattr(module, "setup"):
                if path.stem not in settings:
                    print(f"tools: {path.name} is left out: it needs a [{path.stem}] section in the settings", file=sys.stderr)
                    continue
                module.setup(settings[path.stem])
            # Its own tools only: one it imports from another file is that file's.
            mine = [one for one in vars(module).values() if getattr(one, "is_tool", False) and one.__module__ == module.__name__]
            for one in mine:
                describe(one)  # a parameter of an unknown kind is found now, not when a model is shown it
        except Exception as problem:  # anything wrong in one file leaves that file out, not the folder
            print(f"tools: {path.name} is left out: {type(problem).__name__}: {problem}", file=sys.stderr)
            continue
        for one in mine:
            if one.__name__ in found:
                raise SystemExit(f"tools: the tool {one.__name__} is in both {found[one.__name__]} and {path.name}. Rename one.")
            found[one.__name__] = path.name
        tools += mine
        print(f"tools: {path.name}: {', '.join(one.__name__ for one in mine) or 'no tools'}", file=sys.stderr)
    return tools


def describe(tool: Callable) -> dict:
    """A tool as MCP lists it, read off the function itself. A parameter with no default is required."""
    parameters = inspect.signature(tool, eval_str=True).parameters
    return {
        "name": tool.__name__,
        "description": inspect.getdoc(tool),
        "inputSchema": {
            "type": "object",
            "properties": {name: {"type": TYPES[parameter.annotation]} for name, parameter in parameters.items()},
            "required": [name for name, parameter in parameters.items() if parameter.default is parameter.empty],
        },
    }


def run(tool: Callable, arguments: dict) -> dict:
    """Call one tool and wrap what it returns, or why it failed, as an MCP result."""
    try:
        text, failed = str(tool(**arguments)), False
    except (ValueError, TypeError) as problem:  # the tool refused, or was given arguments it does not take
        text, failed = str(problem), True
    except Exception as problem:  # anything else is a fault in the tool: say so, and show where
        traceback.print_exc()
        text, failed = f"{type(problem).__name__}: {problem}", True
    if len(text) > LONGEST:
        text = text[:LONGEST] + f"\n[cut after {LONGEST} of {len(text)} characters]"
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def answer(name: str, tools: tuple[Callable, ...], message: dict) -> dict | None:
    """The answer to one MCP message, or None for a notification, which gets none."""
    if "id" not in message:
        return None
    reply = {"jsonrpc": "2.0", "id": message["id"]}
    method, asked = message.get("method"), message.get("params") or {}
    by_name = {tool.__name__: tool for tool in tools}
    if method == "initialize":
        reply["result"] = {
            "protocolVersion": asked.get("protocolVersion"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": name, "version": "1"},
        }
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": [describe(tool) for tool in tools]}
    elif method == "tools/call" and asked.get("name") in by_name:
        reply["result"] = run(by_name[asked["name"]], asked.get("arguments") or {})
    else:
        reply["error"] = {"code": -32601, "message": f"{name} cannot do {method} {asked.get('name', '')}".strip()}
    return reply


def arguments(usage: str) -> tuple[list[str], tuple[int, str] | None]:
    """The folders a tool server is given, and the port and key file when it is to answer over HTTP."""
    parser = argparse.ArgumentParser(usage=usage)
    parser.add_argument("folders", nargs="*")
    parser.add_argument("--http", type=int, metavar="PORT", help="answer over HTTP on this port, for a ToolLocker on another machine")
    parser.add_argument("--key-file", metavar="FILE", help="the file holding the key that ToolLocker shows; needed with --http")
    told = parser.parse_args()
    if (told.http is None) != (told.key_file is None):
        parser.error("--http and --key-file go together")
    return told.folders, (told.http, told.key_file) if told.http is not None else None


def serve(name: str, *tools: Callable, http: tuple[int, str] | tuple[int, str, str] | None = None) -> None:
    """Answer MCP messages: over standard input and output until the ToolLocker closes our input,
    or, given `http` as (port, key file), over HTTP until stopped."""
    # Our output carries the messages and nothing else. A stray print goes to the error output.
    out, sys.stdout = sys.stdout, sys.stderr
    if http:
        return serve_http(name, tools, *http)
    for line in sys.stdin:
        # ponytail: one call at a time per tool server. Run calls in threads if that pinches.
        if (answered := answer(name, tools, json.loads(line))) is not None:
            print(json.dumps(answered), file=out, flush=True)


def serve_http(name: str, tools: tuple[Callable, ...], port: int, key_file: str, bind: str = "") -> None:
    """MCP's "Streamable HTTP" transport, as little of it as a ToolLocker needs: each message is
    one POST, and its answer comes back as JSON. A caller without the key is turned away."""
    key = Path(key_file).read_text().strip()
    if not key:
        raise ValueError("The tool-host key file must contain a nonempty key.")
    expected = ("Bearer " + key).encode()

    class Handler(BaseHTTPRequestHandler):
        """HTTP handler for the bounded service protocol."""
        def handle_one_request(self):
            """Assign a fresh, untrusted-client-independent ID for each HTTP attempt."""
            self.ingress_id = secrets.token_hex(16)
            self.response_status = None
            return super().handle_one_request()

        def end_headers(self):
            """Identify normal and generated error responses without retaining tool content."""
            self.send_header("X-Request-Id", self.ingress_id)
            super().end_headers()

        def send_response(self, code, message=None):
            """Remember the status for safe diagnostics before the base class logs it."""
            self.response_status = code
            super().send_response(code, message)

        def log_message(self, format, *args):
            """Keep request IDs and status in journals while excluding URLs, headers and bodies."""
            print(json.dumps({"version": 1, "event": "tool_host_http", "service": "tool-host",
                              "at": time.time(), "request_id": getattr(self, "ingress_id", ""),
                              "status": getattr(self, "response_status", None)}), file=sys.stderr)


        def setup(self):
            """Bound socket reads so an incomplete request cannot monopolize the host."""
            self.request.settimeout(30)
            super().setup()

        def reply(self, status: int, body: dict | None = None) -> None:
            """Write a JSON HTTP response using the tool-host protocol."""
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            # A web page could be made to send here from a browser on this machine. Browsers say where
            # a page came from, and the ToolLocker never does, so anything that says so is refused.
            """Authenticate and dispatch one tool-host JSON-RPC request."""
            if self.headers.get("origin"):
                return self.reply(403, {"error": "Requests from web pages are not taken."})
            if not secrets.compare_digest(self.headers.get("authorization", "").encode(), expected):
                return self.reply(401, {"error": "Missing or wrong key."})
            try:
                length = int(self.headers.get("content-length", 0))
                if not 0 < length <= 16 * 2**20:
                    return self.reply(413, {"error": "Send a nonempty MCP request of at most 16 MiB."})
                message = json.loads(self.rfile.read(length))
                answered = answer(name, tools, message)
            except (ValueError, AttributeError):
                return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "The body must be one JSON-RPC message."}})
            self.reply(202) if answered is None else self.reply(200, answered)

        def do_GET(self) -> None:  # no stream of messages from us is offered
            """Reject unsupported GET requests on the tool-host endpoint."""
            self.reply(405)

        do_DELETE = do_GET  # there are no connections to end: we keep nothing between messages

    # ponytail: one call at a time, as over stdio. ThreadingHTTPServer if two harnesses must overlap.
    listening = HTTPServer((bind, port), Handler)
    print(f"{name}: answering MCP over HTTP on {bind or 'all interfaces'} port {port}", file=sys.stderr, flush=True)
    listening.serve_forever()
