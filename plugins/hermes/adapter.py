"""Hermes CLI invocation and normalization; no shell evaluation or sticky-profile changes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import signal
import tempfile
from pathlib import Path

from plugins._sdk import Adapter, AdapterError, PROFILE, Run
from .reasoning import reasoning_levels

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ROW = re.compile(r"^\s*[◆*]?\s*(.+?)\s+(\S+)\s+(running|stopped)\s+", re.MULTILINE)


def command(settings: dict) -> list[str]:
    """Use a configured venv directly, avoiding activation scripts and shell command strings."""
    if settings.get("venv"):
        return [str(Path(settings["venv"]).expanduser() / "bin/python"), "-m", "hermes_cli.main"]
    return [str(Path(settings.get("cli") or "hermes").expanduser())]


def repository(settings: dict) -> str | None:
    """Resolve a home-relative repository without involving a shell."""
    return str(Path(settings["repository"]).expanduser()) if settings.get("repository") else None


def environment(settings: dict) -> dict:
    """Preserve the installation user's Hermes environment and optional home override."""
    env = dict(os.environ)
    # An adapter launched from an unattended Hermes shell must not inherit bypass flags.
    for name in ("HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS", "HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_SINGLE_QUERY_SESSION"):
        env.pop(name, None)
    if settings.get("hermes_home"):
        env["HERMES_HOME"] = str(Path(settings["hermes_home"]).expanduser())
    return env


async def stop(process: asyncio.subprocess.Process) -> None:
    """Terminate the entire CLI process group, including children ignoring soft cancellation."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def execute(settings: dict, arguments: list[str], timeout: float = 30) -> tuple[int, str]:
    """Run one bounded metadata/control command with closed interactive input."""
    try:
        process = await asyncio.create_subprocess_exec(*command(settings), *arguments,
            cwd=repository(settings), env=environment(settings),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise AdapterError("Hermes executable, venv or repository is unavailable.", 503, "hermes_unavailable") from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode, ANSI.sub("", stdout.decode(errors="replace"))[:100_000]
    finally:
        if process.returncode is None:
            await stop(process)


def parse_profiles(output: str) -> list[dict]:
    """Read JSON inventories when supplied, or canonical IDs from Hermes's CLI table."""
    try:
        document = json.loads(output)
        rows = document if isinstance(document, list) else document.get("profiles", document.get("data", []))
        result = []
        for row in rows:
            name = row if isinstance(row, str) else row.get("id") or row.get("name")
            if isinstance(name, str) and PROFILE.fullmatch(name):
                result.append({"id": name, "name": name})
        return result
    except (ValueError, AttributeError, TypeError):
        result = []
        for match in ROW.finditer(output):
            label = match[1].strip()
            canonical = re.search(r"\(([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)$", label)
            name = canonical[1] if canonical else label
            if PROFILE.fullmatch(name):
                result.append({"id": name, "name": label, "gateway": match[3]})
        if not result and "No profiles found" not in output:
            raise AdapterError("Hermes returned an unrecognized profile inventory.", 502, "profile_inventory_invalid")
        return list({row["id"]: row for row in result}.values())


def query(messages: list[dict], continued: bool, folder: Path) -> tuple[str, list[Path]]:
    """Preserve message roles as data, with only new input on a native continuation."""
    selected = messages
    if continued:
        position = max((i for i, item in enumerate(messages) if item.get("role") == "user"), default=-1)
        if position < 0:
            raise AdapterError("A continuation needs a user message.", 400, "invalid_messages")
        selected = [item for item in messages if item.get("role") == "system"] + messages[position:]
    transcript, images = [], []
    for message in selected:
        content = message.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(parts, list):
            raise AdapterError("Message content must be text or content parts.", 400, "invalid_messages")
        text = []
        for part in parts:
            if not isinstance(part, dict):
                raise AdapterError("Invalid content part.", 400, "invalid_messages")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif part.get("type") == "image_url":
                image_part = part.get("image_url")
                value = image_part.get("url", "") if isinstance(image_part, dict) else ""
                matched = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)", value)
                if not matched or len(images) >= 2:
                    raise AdapterError("Hermes accepts up to two inline PNG/JPEG/WebP images.", 400, "invalid_images")
                try:
                    data = base64.b64decode(matched[2], validate=True)
                except (ValueError, binascii.Error):
                    raise AdapterError("Invalid image encoding.", 400, "invalid_images") from None
                if not data or len(data) > 5 * 1024 * 1024:
                    raise AdapterError("Images must be nonempty and at most 5 MiB.", 400, "invalid_images")
                image = folder / f"image-{len(images)}.{matched[1]}"
                image.write_bytes(data)
                images.append(image)
                text.append("[Attached image]")
            else:
                raise AdapterError("This content type is not supported by the Hermes CLI.", 400, "unsupported_content")
        transcript.append({"role": message.get("role"), "content": "\n".join(text)})
    if len(transcript) == 1 and transcript[0]["role"] == "user":
        return transcript[0]["content"], images
    return "Respond to the latest user input using this conversation and its instructions:\n" + json.dumps(transcript, ensure_ascii=False), images


class Hermes(Adapter):
    """Wrap only the documented Hermes chat, profile and gateway CLI commands."""

    async def models(self, settings: dict) -> list[dict]:
        """Discover profiles without selecting or changing the user's sticky profile."""
        code, output = await execute(settings, ["profile", "list"])
        if code:
            raise AdapterError("Hermes profile discovery failed.", 503, "hermes_unavailable")
        return parse_profiles(output)

    async def health(self, settings: dict) -> dict:
        """Check gateway health/list while keeping CLI chat availability distinct."""
        profile = settings.get("health_profile") or "default"
        if not PROFILE.fullmatch(profile):
            raise AdapterError("Invalid health profile.", 400, "invalid_profile")
        code, status = await execute(settings, ["-p", profile, "gateway", "status"])
        listed, _ = await execute(settings, ["gateway", "list"])
        stopped = bool(re.search(r"not running|inactive|stopped", status, re.I)) and not re.search(r"Gateway is running|Active: active", status)
        return {"state": "stopped" if stopped and not listed else "error" if code or listed else "healthy",
                "chat_available": listed == 0, "profile": profile}

    async def control(self, action: str, settings: dict) -> dict:
        """Start/restart the configured Hermes gateway profile, never the HTTP adapter."""
        if action not in ("start", "restart"):
            raise AdapterError("Unsupported control.", 400, "invalid_action")
        profile = settings.get("health_profile") or "default"
        if not PROFILE.fullmatch(profile):
            raise AdapterError("Invalid health profile.", 400, "invalid_profile")
        code, _ = await execute(settings, ["-p", profile, "gateway", action], timeout=60)
        if code:
            raise AdapterError("Hermes gateway control failed.", 502, "control_failed")
        return {"action": action, "profile": profile, "accepted": True}

    async def events(self, run: Run, settings: dict):
        """Translate actual JSONL events, retaining final text separately from agent previews."""
        if not PROFILE.fullmatch(run.profile):
            raise AdapterError("Invalid Hermes profile.", 400, "invalid_profile")
        options = {**settings, **settings.get("profiles", {}).get(run.profile, {})}
        arguments = ["-p", run.profile, "chat", "--oneshot", "--quiet", "--format", "stream-json", "--source", "tool"]
        if run.native_session:
            arguments += ["--resume", run.native_session]
        elif run.persistent:
            arguments += ["--continue", run.session_title, "--create-if-missing"]
        for setting, flag in (("workspace", "--in"), ("model", "--model"), ("provider", "--provider"),
                              ("skills", "--skills"), ("toolsets", "--toolsets"), ("max_turns", "--max-turns"),
                              ("run_budget", "--run-budget")):
            if options.get(setting):
                value = str(Path(options[setting]).expanduser()) if setting == "workspace" else str(options[setting])
                arguments += [flag, value]
        reasoning = run.reasoning or options.get("reasoning")
        if reasoning:
            levels = reasoning_levels()
            if levels is None:
                raise AdapterError("Hermes reasoning contract is unavailable.", 503, "reasoning_unavailable")
            if reasoning not in levels:
                raise AdapterError("Invalid reasoning effort.", 400, "invalid_reasoning")
            arguments += ["--reasoning", reasoning]
        for setting, flag in (("accept_hooks", "--accept-hooks"), ("worktree", "--worktree"),
                              ("pass_session_id", "--pass-session-id"), ("ignore_rules", "--ignore-rules"),
                              ("ignore_user_config", "--ignore-user-config"), ("safe_mode", "--safe-mode")):
            if options.get(setting):
                arguments.append(flag)
        if options.get("approval", "profile") == "yolo":
            arguments.append("--yolo")
        with tempfile.TemporaryDirectory(prefix="suite-hermes-") as temporary:
            folder = Path(temporary)
            text, images = query(run.messages, bool(run.native_session), folder)
            prompt = folder / "query.txt"
            prompt.write_text(text, encoding="utf-8")
            arguments += ["--query-file", str(prompt)]
            for image in images:
                arguments += ["--image", str(image)]
            try:
                process = await asyncio.create_subprocess_exec(*command(options), *arguments,
                    cwd=repository(options), env=environment(options),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1024 * 1024)
            except OSError:
                raise AdapterError("Hermes executable, venv or repository is unavailable.", 503, "hermes_unavailable") from None

            async def drain():
                """Drain the owned Hermes process output into bounded adapter events."""
                while await process.stderr.read(65536):
                    pass  # diagnostics can contain prompts/credentials; they are not logged or returned

            draining = asyncio.create_task(drain())
            finished = False
            diagnostics = 0
            try:
                async with asyncio.timeout(options.get("run_budget", 300) + 10):
                    async for line in process.stdout:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            # Hermes/customizations can print startup and memory-service
                            # diagnostics between JSONL events. Bound and discard them;
                            # malformed event objects and missing terminal results still fail.
                            if not line.lstrip().startswith(b"{"):
                                diagnostics += len(line)
                                if diagnostics <= 65536:
                                    continue
                            raise AdapterError("Hermes emitted invalid JSONL.", 502, "invalid_event_stream") from None
                        if not isinstance(event, dict):
                            raise AdapterError("Invalid Hermes event.", 502, "invalid_event_stream")
                        kind = event.get("type")
                        if event.get("session_id"):
                            yield {"type": "session", "native_id": str(event["session_id"])}
                        if kind == "result":
                            if event.get("exit_code") or event.get("error"):
                                raise AdapterError("Hermes run failed or required interactive approval.", 502, "hermes_failed")
                            finished = True
                            yield {"type": "result", "text": event.get("text") or ""}
                        elif kind == "text":
                            yield {"type": "preview", "text": event.get("text") or ""}
                        elif kind in ("reasoning", "reasoning_delta"):
                            yield {"type": "reasoning", "text": event.get("text") or event.get("reasoning") or ""}
                        elif kind in ("tool_use", "tool_result", "system"):
                            yield {key: value for key, value in event.items() if key not in ("tokens", "usage", "cost", "timestamp")}
                        elif kind not in ("usage", "tokens", "cost"):
                            yield {"type": "status", "name": str(kind or "unknown")}
                    code = await process.wait()
                    if code or not finished:
                        raise AdapterError("Hermes ended without a successful final result.", 502, "hermes_failed")
            except TimeoutError:
                raise AdapterError("Hermes exceeded its run budget.", 504, "run_timeout") from None
            finally:
                if process.returncode is None:
                    await stop(process)
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)
