"""The ToolLocker: one MCP server in front of many, behind the suite's accounts, levels and log.

A harness is given one address, such as http://10.77.10.42:8443/mcp, and an API key. It asks
what tools there are and calls them, exactly as it would any MCP server. The door works out who
is asking, offers only the tools their group's level allows, passes each call to the server
that owns the tool, and writes what happened to the event log.

    servers.py   starts the MCP servers and talks to them
    tools/       the small MCP servers written here

Who is asking is turnstile's to say, on the front machine: it checks the caller's key and sends
a signed statement in its place (see loadcoach/statement.py). The gateway runs on front with
front-owned tool records; tool files execute on their configured owning hosts. Its configured
event store uses the shared audit-log implementation rather than a separate identity authority.
"""

from __future__ import annotations

from loadcoach import observability as observation

import json
import re
import secrets
import asyncio
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated

from db import DatabaseRegistry, DatabaseSource, migrate
from db.maintenance import Barrier, Gate
from db.schemas import TOOL_SCHEMAS
import typer
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from loadcoach.web import diagnostic_page
from starlette.routing import Route

from loadcoach.app import TRACE
from loadcoach.config import CLI, data_folder, kept_by_loadcoach, read_settings, said_in
from loadcoach.log import Log
from loadcoach.groups import LEVELS
from loadcoach.statement import Statements, user_of
from loadcoach.settings import Changes, changed_for, laid_over, said, taken
from loadcoach.users import User
from toollocker.servers import Remote, Server, ToolProblem

TEST_PAGE = Path(__file__).with_name("test.html")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")  # a server or a tool set: it becomes part of names and addresses
RETRY = 30  # seconds between tries of a server that did not start
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")  # of MCP. For tools they do not differ.


@dataclass(frozen=True)
class Config:
    """Gateway settings; the signing key and store come from its owning host's file."""

    servers: dict[str, dict]  # name -> command, or url with key_file/public; level and description
    toolsets: dict[str, dict] = field(default_factory=dict)  # name -> {"tools": names or patterns}
    tool_levels: dict[str, str] = field(default_factory=dict)  # tool, by the name a harness sees -> its own level, in place of its server's
    machine_key_file: str = ""  # the key turnstile signs with. Empty: nobody is known, and every call is refused
    database: DatabaseSource = ":memory:"
    databases: dict = field(default_factory=dict)  # optional named identity store binding
    host: str = "127.0.0.1"
    port: int = 8443
    call_timeout: float = 120  # seconds one tool call may take
    path: str = ""  # the settings file itself
    file: dict = field(default_factory=dict)  # what that file says, before anything changed on the site is laid over it
    says: dict = field(default_factory=dict)  # the comment above each setting in the file, shown beside it on the site


def shared_store(path: Path, told: dict) -> tuple[str, DatabaseSource]:
    """Resolve the owning host's signing key and store without opening model state.

    `front` selects WeightRoom's accounts/event store. Legacy installations keep
    using LoadCoach's store. Remote tools never receive this store or signing key.
    """
    if told.get("front"):
        if "loadcoach" in told:
            raise SystemExit(f"{path}: choose front or loadcoach, not both")
        shared = path.parent / told["front"]
        main = read_settings(shared)
        store = DatabaseRegistry.from_config(main.get("databases"), shared.parent,
                    {"weightroom.accounts": data_folder(shared, main) / "accounts.db"}).database("weightroom.accounts")
        key = shared.parent / main.get("turnstile", {}).get("machine_key_file", "../keys/machine.key")
        return str(key.resolve()), store
    shared = path.parent / told.get("loadcoach", "loadcoach.toml")
    main, store, _ = kept_by_loadcoach(shared)
    if main.get('front_toollocker'):
        raise SystemExit(f"{path}: ToolLocker moved to front; use front's toollocker.toml")
    key = str(shared.parent / main["machine_key_file"]) if main.get("machine_key_file") else ""
    return key, store


def load_config(path: Path) -> Config:
    """Read the TOML file, and lay over it what was changed on the site. Anything wrong with the file stops startup with a message naming the cause."""
    told = read_settings(path)
    shared = shared_store(path, told)[1]
    identity = (DatabaseRegistry.from_config(told.get("databases"), path.parent, {"toollocker.identity": shared})
                .database("toollocker.identity") if told.get("databases") else shared)
    changed = changed_for("toollocker", identity)
    try:
        return config_from(path, laid_over(told, changed, "toollocker"), told)
    except SystemExit as problem:
        if not changed:
            raise
        print(f"toollocker: a setting changed on the site cannot be used: {problem}. Starting with the file's own settings.", file=sys.stderr, flush=True)
        return config_from(path, told, told)


def config_from(path: Path, data: dict, file: dict | None = None) -> Config:
    """The settings that `data` makes, checked. `file` is what the file itself says, kept to be shown."""
    data, file = dict(data), data if file is None else file
    # From LoadCoach's file the door takes two things only: the machine key, and where the log is
    # kept. The rest of that file is LoadCoach's to check. A model whose file has gone missing
    # stops LoadCoach from starting, and must not stop the ToolLocker.
    key_file, database = shared_store(path, data)
    data.pop("loadcoach", None)
    data.pop("front", None)
    try:
        config = Config(**{"servers": {}, **data, "machine_key_file": key_file, "database": database, "path": str(path), "file": file, "says": said_in(path)})
        if type(config.call_timeout) not in (int, float) or config.call_timeout <= 0:
            raise ValueError("call_timeout is a number of seconds above 0")
    except (TypeError, ValueError) as problem:  # an unknown key, or a group that makes no sense
        raise SystemExit(f"{path}: {problem}") from None
    for name, server in config.servers.items():
        where = f"{path}: server {name!r}"
        if not NAME.fullmatch(name):
            raise SystemExit(f"{where}: a server's name is lowercase letters, digits and dashes")
        if not isinstance(server, dict) or server.keys() - {"command", "url", "key_file", "public", "level", "description"}:
            raise SystemExit(f"{where} may set `command`, `url`, `key_file`, `public`, `level` and `description`, nothing else")
        if not isinstance(server.get("description", ""), str):
            raise SystemExit(f"{where}: `description` is text")
        if server.get("level", "normal") not in LEVELS:
            raise SystemExit(f"{where}: level must be one of {', '.join(LEVELS)}")
        if "url" in server:  # a server on another machine
            if "command" in server or not isinstance(server["url"], str) or not server["url"].startswith(("http://", "https://")):
                raise SystemExit(f"{where}: `url` is an address beginning http:// or https://, and is set instead of `command`")
            if server.get("public", False) is True:
                if not server["url"].startswith("https://") or "key_file" in server:
                    raise SystemExit(f"{where}: `public = true` needs HTTPS and no `key_file`")
            elif "public" in server:
                raise SystemExit(f"{where}: `public` may only be true for an unauthenticated HTTPS server")
            elif not isinstance(server.get("key_file"), str) or not server["key_file"]:
                raise SystemExit(f"{where} needs `key_file`: the file holding the key the door shows that server")
            continue
        if "key_file" in server or "public" in server:
            raise SystemExit(f"{where}: `key_file` and `public` go with `url`, not `command`")
        command = server.get("command")
        if not (isinstance(command, list) and command and all(isinstance(part, str) for part in command)):
            raise SystemExit(f"{where} needs `command`: a list holding the program and its arguments")
    for name, toolset in config.toolsets.items():
        tools = toolset.get("tools") if isinstance(toolset, dict) and toolset.keys() == {"tools"} else None
        if not NAME.fullmatch(name) or not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise SystemExit(f"{path}: tool set {name!r} needs a lowercase name and `tools`, a list of names")
    if not isinstance(config.tool_levels, dict) or not all(isinstance(tool, str) and level in LEVELS for tool, level in config.tool_levels.items()):
        raise SystemExit(f"{path}: tool_levels gives each tool, by the name a harness sees, one of {', '.join(LEVELS)}")
    return config


def note_changes(tools: dict[str, tuple[Server, dict]], log: Log) -> None:
    """Write to the log every tool that is new, or whose description has changed since last time.

    A tool's description is text a model reads and acts on, so a change to it matters as much as
    a change to code. What each tool looked like last time is kept in a table of the door's own,
    in the same file as the log and through the log's own connection to it.
    """
    migrate(log.db.database, log.db, "toollocker.tools", TOOL_SCHEMAS)
    for name, (_, described) in tools.items():
        now = json.dumps(described, sort_keys=True)
        before = log.db.execute("SELECT schema FROM tool_schemas WHERE tool = ?", (name,)).fetchone()
        if before is None or before[0] != now:
            text = {"before": before and json.loads(before[0]), "after": described}
            log.add("tool", "changed" if before else "new", text=text, tool=name)
            log.db.execute("INSERT INTO tool_schemas (tool, schema) VALUES (?, ?) ON CONFLICT(tool) DO UPDATE SET schema=excluded.schema", (name, now))


def build_app(config: Config) -> Starlette:
    """Build the web app: the one MCP address, and the servers behind it."""
    databases = DatabaseRegistry.from_config(
        config.databases,
        Path(config.path).parent if config.path else Path.cwd(),
        {"toollocker.identity": config.database},
    )
    identity = databases.database("toollocker.identity")
    gate = Gate(config.path)
    log = Log(identity)
    # A key file is found beside the settings file unless its path is a full one.
    folder = Path(config.path).parent if config.path else Path.cwd()
    servers = [Remote(name, server["url"], str(folder / server["key_file"]) if "key_file" in server else None, server.get("level", "normal")) if "url" in server
               else Server(name, server["command"], server.get("level", "normal")) for name, server in config.servers.items()]  # fmt: skip
    # Every tool on offer, by the name a harness sees: the server's name, two underscores, then
    # the tool's own name. Filled in when the servers have started.
    tools: dict[str, tuple[Server | Remote, dict]] = {}
    # Servers that did not start, and when each was last tried. Each is tried again when a harness
    # lists tools, at most every RETRY seconds, so a machine that was asleep joins when it wakes.
    missing: dict[Server | Remote, float] = {}

    async def bring_up(server: Server | Remote) -> None:
        """Start one server and offer its tools, or note that it is missing."""
        missing[server] = time.monotonic()
        try:
            async with server.starting:
                if server.dead():
                    await server.stop()
                    await server.start()
        except ToolProblem as problem:  # the door opens without it, and says so
            print(f"toollocker: {problem}", file=sys.stderr)
            log.add("tool", "server-failed", server=server.name, why=str(problem))
            return
        missing.pop(server, None)
        # A server's description is put in front of each of its tools', so that a model can tell
        # the same tool on two machines apart: "nas: the family files. Read a text file, ..."
        about = config.servers.get(server.name, {}).get("description")
        for described in server.tools:
            if about:
                described = {**described, "description": f"{server.name}: {about}. {described.get('description') or ''}".rstrip()}
            tools[f"{server.name}__{described['name']}"] = (server, described)

    def level_of(name: str, server: Server | Remote) -> str:
        """A tool's level: its own if one is set, otherwise its server's."""
        return config.tool_levels.get(name, server.level)

    def failed(why: str) -> dict:
        """A tool result that says the call did not work, in the words a model will read."""
        return {"content": [{"type": "text", "text": why}], "isError": True}

    async def call(request: Request, user: User, name: str, server: Server | Remote, tool: str, arguments: dict) -> dict:
        """Run one tool for one user, and record it. Returns the result to hand back."""
        # The level of this call is the tool's own, raised by what the caller says and by what
        # its piece of work has already been. It sticks to the piece of work, as at LoadCoach.
        asked = request.headers.get("x-loadcoach-sensitivity", "normal")
        trace = request.headers.get("x-loadcoach-trace") or request.headers.get("mcp-session-id")
        if asked not in LEVELS or (trace and not TRACE.fullmatch(trace)):
            return failed("X-LoadCoach-Sensitivity or X-LoadCoach-Trace is not in a form that is understood.")
        level = max(level_of(name, server), asked, (trace and log.trace_level(user.name, trace)) or "normal", key=LEVELS.index)
        about = (user.name, request.client.host if request.client else "?", None, trace, level)
        if LEVELS.index(level) > LEVELS.index(user.group.max_sensitivity):
            log.add("refused", "403", *about, tool=name, why=f"may not work at the {level} level")
            return failed(f"Your group may not work at the {level} level.")
        if trace:
            log.set_trace_level(user.name, trace, level)

        keeping = level != "normal"  # sensitive work: the arguments and the result are kept too
        try:
            entry = log.add("tool", "called", *about, text={"arguments": arguments} if keeping else None, must=keeping, tool=name)
        except Exception as problem:
            if not log.db.database.is_error(problem):
                raise
            return failed(f"This call is marked {level} and could not be recorded, so it was not run.")
        started = time.monotonic()
        try:
            with observation.span('tool_execution', tool_executed=False):
                result = await server.call(tool, arguments, config.call_timeout)
                observation.annotate(tool_executed=True)
                if result.get('isError'):
                    observation.annotate(outcome='error', error_code='tool_failed')
        except ToolProblem as problem:
            result = failed(str(problem))
        how = "failed" if result.get("isError") else "returned"
        try:
            log.add("tool", how, *about, text={"result": result} if keeping else None, must=keeping,
                    tool=name, of=entry, took=round(time.monotonic() - started, 2))  # fmt: skip
        except Exception as problem:
            if not log.db.database.is_error(problem):
                raise
            return failed(f"This call is marked {level} and its result could not be recorded, so it is withheld.")
        return result

    async def mcp(request: Request) -> Response:
        """The one address. Each POST carries one MCP message and gets its answer back as JSON."""
        # Turnstile has checked the caller's key, and says who they are in a signed statement.
        said = getattr(request.state, "said", None)
        if not said or not said.get("caller"):
            log.add("refused", "401", "", request.client.host if request.client else "?", path=request.url.path, why="Not sent through turnstile.")
            return JSONResponse({"error": "Ask through turnstile, on the front machine. It checks your key and says here who you are."}, status_code=401)
        user = user_of(said["caller"])
        toolset = request.path_params.get("toolset")
        # A harness may also choose tool sets by header, on any address: "code,email" offers the tools of either.
        chosen = [one.strip() for one in request.headers.get("x-toollocker-sets", "").split(",") if one.strip()]
        if unknown := [one for one in [toolset, *chosen] if one is not None and one not in config.toolsets]:
            return JSONResponse({"error": f"There is no tool set named {unknown[0]!r}."}, status_code=404)
        try:
            message = json.loads(await request.body())
            method, params = message.get("method"), message.get("params")
        except (ValueError, AttributeError):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "The body must be one JSON-RPC message."}}, status_code=400)  # fmt: skip
        if method is None or "id" not in message:
            return Response(status_code=202)  # a notification: taken, and there is nothing to say back
        params = params if isinstance(params, dict) else {}
        if method == "tools/list":
            for server, tried in list(missing.items()):
                if time.monotonic() - tried >= RETRY:
                    # ponytail: the harness waits while a down server is tried, up to its connect timeout. Try in the background if that bites.
                    missing[server] = time.monotonic()  # so that a second harness listing now does not try it too
                    await bring_up(server)
                    if server not in missing:
                        note_changes(tools, log)

        def answer(result: dict | None = None, error: tuple[int, str] | None = None, headers: dict | None = None) -> Response:
            """Build the JSON-RPC result or error response for the current MCP request."""
            body = {"error": {"code": error[0], "message": error[1]}} if error else {"result": result}
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], **body}, headers=headers)

        def in_set(name: str, toolset: str) -> bool:
            """Check whether a tool belongs to the permitted requested tool sets."""
            return any(fnmatchcase(name, pattern) for pattern in config.toolsets[toolset]["tools"])

        # What this caller may use here: tools at or below their group's level, that their group and
        # account are shown, in this address's tool set and in one of the sets the header chose.
        allowed = {
            name: tool
            for name, tool in tools.items()
            if LEVELS.index(level_of(name, tool[0])) <= LEVELS.index(user.group.max_sensitivity)
            and user.group.may_see_tool(name)
            and (toolset is None or in_set(name, toolset))
            and (not chosen or any(in_set(name, one) for one in chosen))
        }
        if method == "initialize":
            version = params.get("protocolVersion")
            return answer(
                {
                    "protocolVersion": version if version in VERSIONS else VERSIONS[-1],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "toollocker", "version": "1"},
                },
                # The harness sends this back with every later message. It is this connection's
                # piece of work in the log, for a harness that names none of its own.
                headers={"mcp-session-id": "door-" + secrets.token_hex(8)},
            )
        if method == "ping":
            return answer({})
        if method == "tools/list":
            return answer({"tools": [{**described, "name": name} for name, (_, described) in allowed.items()]})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if name not in allowed or not isinstance(arguments, dict):
                # A tool the caller may not use is answered exactly like one that is not there.
                return answer(error=(-32602, f"Unknown tool: {name}"))
            server, described = allowed[name]
            return answer(await call(request, user, name, server, described["name"], arguments))
        return answer(error=(-32601, f"The ToolLocker does not do {method}."))

    async def test_page(request: Request) -> Response:
        # No key needed to load it. The page holds no secrets, and every message it sends
        # goes through `mcp` like any harness's.
        """Serve the static MCP exerciser without granting access to any tool."""
        return diagnostic_page(TEST_PAGE)

    async def in_step() -> None:
        """Every few seconds, take on what was changed in the settings on the site, and say what the door runs with. See loadcoach/settings.py."""
        nonlocal config
        changes, last = Changes(identity), None
        while True:
            await gate.wait()
            changed = changes.of("toollocker")
            if changed != last:
                config, refused = taken("toollocker", config, changed, config_from)
                running = {**config.file, **{name: getattr(config, name) for name in ("host", "port", "call_timeout", "servers", "toolsets", "tool_levels")}}
                changes.seen("toollocker", said(running, config.file, config.says), refused)
                last = changed
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        """Start background workers and release service resources on shutdown."""
        for server in servers:
            await bring_up(server)
        note_changes(tools, log)
        log.add("toollocker", "started", tools=sorted(tools))
        following = asyncio.create_task(in_step())
        try:
            yield
        finally:
            following.cancel()
            await asyncio.gather(following, return_exceptions=True)
            for server in servers:
                await server.stop()
                if isinstance(server, Remote):
                    await server.client.aclose()
            log.add("toollocker", "stopped")
            await observation.shutdown(app)

    app = Starlette(
        lifespan=lifespan,
        routes=[Route("/", test_page), Route("/mcp", mcp, methods=["POST"]), Route("/mcp/{toolset}", mcp, methods=["POST"])],
    )
    app.state.databases = databases
    app.add_middleware(Barrier, gate=gate)
    app.add_middleware(Statements, key_file=config.machine_key_file)  # who each caller is, as turnstile signed it
    observation.instrument(app, "toollocker", log)
    return app


cli = typer.Typer(**CLI)


@cli.command()
def serve(config: Annotated[Path, typer.Option(metavar="FILE", help="The settings file.")] = Path("settings/toollocker.toml")) -> None:
    # Each paragraph is one line here, because Typer keeps the line breaks it is given.
    """toollocker: one MCP server in front of many. A harness connects here and gets the tools it may use.

    It starts the tool servers named in its settings, each as a program of its own, and offers their tools at /mcp, or a chosen few at /mcp/TOOLSET. Every call is checked against the caller's key and written to the event log, with the tool's own description watched for changes.

    It has no commands. It runs until stopped, normally as a service: systemctl --user status toollocker

    Who may connect is turnstile's to say, on the front machine: a harness asks through it, with its key, and turnstile signs who it is. A page for trying a tool by hand is at its own address, /.

    Every setting is explained in settings/examples/toollocker.toml and settings/defaults/toollocker.toml.
    """
    settings = load_config(config)
    # No access log: a key may be in the address, and each call is in the event log already.
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, access_log=False)


def main() -> None:
    """Run the configured authenticated MCP gateway."""
    cli()
