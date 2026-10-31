"""The MCP servers behind the door: starting each one and talking to it.

Most servers are programs the door runs. The two talk over the program's standard input and
output, one JSON message per line, which is MCP's "stdio" transport. Every request the door
sends carries a number, and the answer comes back with the same number.

A server on another machine is reached by its address instead, with MCP's "Streamable HTTP"
transport: each message is one POST, and the answer comes back as JSON or as a short stream of
events. The door shows a key so the server knows who is asking.
"""

from __future__ import annotations

import asyncio
import json
import sys
from asyncio.subprocess import PIPE
from pathlib import Path

import httpx

PROTOCOL = "2025-06-18"  # the version of MCP the door speaks to the servers it starts
LONGEST_LINE = 16 * 2**20  # bytes in one message from a server; a tool's whole result is one line


class ToolProblem(Exception):
    """A tool server could not answer: it would not start, it stopped, it was too slow, or it said no."""


async def catalogue(server) -> list[dict]:
    """Read bounded MCP tool pages, rejecting malformed tools and repeated cursors."""
    tools, names, cursors = [], set(), set()
    cursor, size = None, 0
    while True:
        page = await server.ask("tools/list", {"cursor": cursor} if cursor else {})
        listed = page.get("tools")
        if not isinstance(listed, list) or len(tools) + len(listed) > 4096:
            raise ToolProblem(f"The {server.name} tool catalogue must contain at most 4096 tools.")
        size += len(json.dumps(page).encode())
        if size > LONGEST_LINE:
            raise ToolProblem(f"The {server.name} tool catalogue exceeds 16 MiB.")
        for tool in listed:
            if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                    or not tool["name"] or tool["name"] in names
                    or not isinstance(tool.get("inputSchema", {}), dict)
                    or not isinstance(tool.get("description", ""), str)):
                raise ToolProblem(f"The {server.name} tool catalogue contains an invalid or duplicate tool.")
            names.add(tool["name"])
            tools.append(tool)
        cursor = page.get("nextCursor")
        if cursor is None or cursor == "":
            return tools
        if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 128:
            raise ToolProblem(f"The {server.name} tool catalogue returned an invalid or repeated cursor.")
        cursors.add(cursor)


class Server:
    """One MCP server run as a program, and the tools it says it has."""

    def __init__(self, name: str, command: list[str], level: str) -> None:
        """Configure one child MCP server and initialize request correlation state."""
        self.name, self.command, self.level = name, command, level
        self.process: asyncio.subprocess.Process | None = None
        self.listener: asyncio.Task | None = None
        self.tools: list[dict] = []  # each tool as the server describes it
        self.waiting: dict[int, asyncio.Future] = {}  # request number -> where its answer goes
        self.asked = 0  # the number of the last request sent
        self.starting = asyncio.Lock()  # so that two calls do not both restart a stopped server

    def dead(self) -> bool:
        """Return whether the child or its response reader is unavailable."""
        return self.process is None or self.process.returncode is not None or self.listener is None or self.listener.done()

    async def start(self) -> None:
        """Start the program, greet it as MCP requires, and ask what tools it has."""
        try:
            self.process = await asyncio.create_subprocess_exec(*self.command, stdin=PIPE, stdout=PIPE, limit=LONGEST_LINE)
        except OSError as problem:
            raise ToolProblem(f"The {self.name} tool server could not be started: {problem}") from None
        self.listener = asyncio.create_task(self.listen(self.process))
        try:
            await self.ask("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "toollocker", "version": "1"}})
            self.tell("notifications/initialized")
            self.tools = await catalogue(self)
        except BaseException:
            await self.stop()
            raise
        print(f"toollocker: started {self.name} with {len(self.tools)} tools", file=sys.stderr)

    async def listen(self, process: asyncio.subprocess.Process) -> None:
        """Hand each answer to whoever is waiting for it. Runs for as long as the program does."""
        try:
            async for line in process.stdout:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict) or type(message.get("id")) is not int:
                        continue
                    waiting = self.waiting.pop(message["id"], None) if "method" not in message else None
                except (ValueError, KeyError, TypeError):
                    continue  # not an answer to anything we asked
                if waiting and not waiting.done():
                    waiting.set_result(message)
        finally:
            # The program has ended, or sent something unreadable. Nobody still waiting will get
            # an answer, so tell them, and make sure the program is gone.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # exited between the status check and signal
            await process.wait()
            for waiting in self.waiting.values():
                if not waiting.done():
                    waiting.set_exception(ToolProblem(f"The {self.name} tool server stopped."))

    def tell(self, method: str, params: dict | None = None, number: int | None = None) -> None:
        """Send one message. With a number it is a request, and an answer is expected."""
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if number is not None:
            message["id"] = number
        self.process.stdin.write(json.dumps(message).encode() + b"\n")

    async def ask(self, method: str, params: dict, timeout: float = 30) -> dict:
        """Send one request and wait for its answer. Raises ToolProblem if none comes, or it is an error."""
        if self.dead():
            raise ToolProblem(f"The {self.name} tool server stopped.")
        self.asked += 1
        number, answer = self.asked, asyncio.get_running_loop().create_future()
        self.waiting[number] = answer
        try:
            async with asyncio.timeout(timeout):
                self.tell(method, params, number)
                await self.process.stdin.drain()
                message = await answer
        except TimeoutError:
            await self.stop()  # join the old reader before a new child gets its waiters
            raise ToolProblem(f"The {self.name} tool server took longer than {timeout:g} seconds and was stopped.") from None
        except (OSError, RuntimeError):
            raise ToolProblem(f"The {self.name} tool server stopped while sending a request.") from None
        finally:
            self.waiting.pop(number, None)
            if not answer.done():
                answer.cancel()
        if "error" in message:
            problem = message["error"]
            raise ToolProblem(str(problem.get("message", problem) if isinstance(problem, dict) else problem))
        result = message.get("result") or {}
        if not isinstance(result, dict):
            raise ToolProblem(f"The {self.name} tool server returned a result that is not an object.")
        return result

    async def call(self, tool: str, arguments: dict, timeout: float) -> dict:
        """Run one tool and return its result as the server gave it. A stopped server is started again first."""
        async with self.starting:
            if self.dead():
                await self.stop()
                await self.start()
        return await self.ask("tools/call", {"name": tool, "arguments": arguments}, timeout)

    async def stop(self) -> None:
        """End the program: close its input, which asks it to finish, and insist if it does not."""
        process, listener = self.process, self.listener
        if process and process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        if listener:
            await asyncio.gather(listener, return_exceptions=True)


class Remote:
    """One MCP server on another machine, reached by its address, and the tools it says it has."""

    def __init__(self, name: str, url: str, key_file: str | None, level: str) -> None:
        """Configure a remote MCP client and its session and authorization state."""
        self.name, self.url, self.key_file, self.level = name, url, key_file, level
        self.tools: list[dict] = []
        self.session: str | None = None  # the server's name for our connection, once it has given one
        self.headers: dict[str, str] = {}
        self.ready = False
        self.asked = 0
        self.starting = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5))

    def dead(self) -> bool:
        """Return whether the remote server needs its MCP session initialized again."""
        return not self.ready  # never greeted, or the server has forgotten us

    async def start(self) -> None:
        """Greet the server as MCP requires, and ask what tools it has."""
        self.session, self.headers, self.ready = None, {}, False
        if self.key_file is not None:
            try:
                key = Path(self.key_file).read_text().strip()
            except OSError as problem:
                raise ToolProblem(f"The {self.name} tool server's key could not be read: {problem}") from None
            if not key:
                raise ToolProblem(f"The {self.name} tool server's key is empty.")
            self.headers["authorization"] = "Bearer " + key
        try:
            hello = await self.ask("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "toollocker", "version": "1"}})
            self.headers["mcp-protocol-version"] = hello.get("protocolVersion", PROTOCOL)
            await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools = await catalogue(self)
        except ToolProblem:
            self.headers = {}
            raise
        self.ready = True
        print(f"toollocker: reached {self.name} with {len(self.tools)} tools", file=sys.stderr)

    async def send(self, message: dict) -> dict | None:
        """POST one message. Returns the answer to it, or None when the server has nothing to say back."""
        headers = {**self.headers, "accept": "application/json, text/event-stream"}
        if self.session:
            headers["mcp-session-id"] = self.session
        try:
            async with self.client.stream("POST", self.url, json=message, headers=headers) as reply:
                if reply.status_code == 404 and self.session:
                    self.ready = False  # the server has forgotten our connection. The next call greets it again.
                    raise ToolProblem(f"The {self.name} tool server ended the connection. Try again.")
                if reply.status_code in (401, 403):
                    self.ready = False  # reread a rotated key on the next call
                    raise ToolProblem(f"The {self.name} tool server refused the door's key.")
                if reply.status_code >= 400:
                    raise ToolProblem(f"The {self.name} tool server answered {reply.status_code}.")
                self.session = reply.headers.get("mcp-session-id", self.session)
                if "id" not in message or reply.status_code == 202:
                    return None
                streamed = reply.headers.get("content-type", "").startswith("text/event-stream")
                pending, data, received = bytearray(), [], 0
                async for chunk in reply.aiter_bytes():
                    received += len(chunk)
                    if received > LONGEST_LINE:
                        raise ToolProblem(f"The {self.name} tool server's answer exceeds 16 MiB.")
                    pending.extend(chunk)
                    if streamed:
                        lines = pending.split(b'\n')
                        pending = lines.pop()
                        for line in lines:
                            line = line.rstrip(b'\r')
                            if line.startswith(b'data:'):
                                data.append(line[5:].strip())
                            elif not line and data:
                                event, data = json.loads(b'\n'.join(data)), []
                                if isinstance(event, dict) and event.get("id") == message["id"] and "method" not in event:
                                    return event
                if streamed:
                    raise ToolProblem(f"The {self.name} tool server ended its answer without one.")
                return json.loads(pending)
        except httpx.HTTPError as problem:
            self.ready = False  # it may be asleep or gone. The next call tries again from the start.
            raise ToolProblem(f"The {self.name} tool server could not be reached: {problem}") from None
        except ValueError:
            raise ToolProblem(f"The {self.name} tool server sent something that is not JSON.") from None

    async def ask(self, method: str, params: dict, timeout: float = 30) -> dict:
        """Send one request and wait for its answer. Raises ToolProblem if none comes, or it is an error."""
        self.asked += 1
        number = self.asked
        try:
            message = await asyncio.wait_for(self.send({"jsonrpc": "2.0", "id": number, "method": method, "params": params}), timeout)
        except TimeoutError:
            raise ToolProblem(f"The {self.name} tool server took longer than {timeout:g} seconds.") from None
        if not isinstance(message, dict):
            raise ToolProblem(f"The {self.name} tool server returned no answer to {method}.")
        if message.get("id") != number or "method" in message:
            raise ToolProblem(f"The {self.name} tool server answered a different request.")
        if "error" in message:
            problem = message["error"]
            raise ToolProblem(str(problem.get("message", problem) if isinstance(problem, dict) else problem))
        result = message.get("result") or {}
        if not isinstance(result, dict):
            raise ToolProblem(f"The {self.name} tool server returned a result that is not an object.")
        return result

    call = Server.call  # the same as a program's: greet the server again first if it has forgotten us

    async def stop(self) -> None:
        """End the current remote MCP session and forget its authorization headers."""
        if self.session and self.ready:
            try:  # tell the server our connection is over. It need not listen.
                await self.client.delete(self.url, headers={**self.headers, "mcp-session-id": self.session}, timeout=3)
            except httpx.HTTPError:
                pass
        self.headers, self.ready = {}, False
