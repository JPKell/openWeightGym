"""Hermes run API bridge, pinned to the capability contract inspected on the front host.

Only run.completed.output is speech. Runs are submitted once. Stop waits for a terminal
state before another run may own the conversation; an uncertain outcome is never retried.
"""

import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile

from voice.protocol import PLAYBACK_INSTRUCTIONS, speakable

INSTRUCTIONS = "This is a voice call. Answer in one or two short sentences of plain prose, with no lists, Markdown or URLs."


def configuration(gateway):
    """Snapshot editable voice-agent choices without exposing machine paths or credentials to edits."""
    settings = dict(gateway["hermes"])
    for name in ("model", "system_prompt", "max_turns", "context_tokens", "max_output_tokens", "reasoning_effort"):
        value = gateway.get("hermes_" + name)
        if value is not None and (name != "model" or value):
            settings[name] = value
    return settings


class Bridge:
    """Bounded asynchronous connection to the configured Hermes voice process."""
    def __init__(self, http, settings, session_id):
        """Configure the private Hermes bridge and bounded request transport."""
        self.http, self.settings, self.session_id = http, settings, session_id
        self.url = settings.get("url", "").rstrip("/")
        api_key = Path(settings["key_file"]).read_text().strip() if settings.get("key_file") else secrets.token_urlsafe(32)
        self.headers = {"authorization": "Bearer " + api_key,
                        "X-Hermes-Session-Key": "voice:" + session_id}
        self.run_id = None
        self.lock = asyncio.Lock()
        self.history = []
        self.uncertain = False
        self.process = None
        self.home = None
        self.delivery = {}

    async def request(self, method, path, **kwargs):
        """Send one bounded voice-agent request over the private Hermes bridge."""
        result = await self.http.request(method, self.url + path, headers=self.headers, **kwargs)
        result.raise_for_status()
        return result.json()

    async def ready(self):
        """Prepare the private Hermes API process when configured and wait for readiness."""
        if self.settings.get("python"):
            # One supported API process per call gives memory, tools and workspace a real
            # boundary. It reads no credentials or configuration from the owner's Hermes.
            self.home = tempfile.TemporaryDirectory(prefix="voice-hermes-")
            home = Path(self.home.name)
            (home / "workspace").mkdir()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            self.url = f"http://127.0.0.1:{port}"
            config = {
                "model": {"default": self.settings["model"], "provider": "custom",
                          "base_url": self.settings["provider_url"], "api_key": "${VOICE_MODEL_KEY}"},
                "agent": {"max_turns": self.settings.get("max_turns", 8),
                          "context_tokens": self.settings.get("context_tokens", 8192),
                          "reasoning_effort": self.settings.get("reasoning_effort", "none")},
                "platforms": {"api_server": {"enabled": True}},
                "platform_toolsets": {"api_server": self.settings.get("toolsets", ["search"])},
                "memory": {"memory_enabled": False, "user_profile_enabled": False},
                "terminal": {"cwd": str(home / "workspace")},
                "web": {"search_backend": "searxng", "keyless_fallback": False, "keyless_rescue": False},
                "gateway": {"api_server": {"max_concurrent_runs": 1}},
            }
            if self.settings.get("max_output_tokens", 0) > 0:
                config["model"]["max_tokens"] = self.settings["max_output_tokens"]
            (home / "config.yaml").write_text(json.dumps(config))
            (home / "config.yaml").chmod(0o600)
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "USER", "XDG_RUNTIME_DIR")}
            env.update(HERMES_HOME=str(home), API_SERVER_KEY=self.headers["authorization"][7:],
                       API_SERVER_HOST="127.0.0.1", API_SERVER_PORT=str(port),
                       VOICE_MODEL_KEY=Path(self.settings["provider_key_file"]).read_text().strip(),
                       SEARXNG_URL=self.settings.get("search_url", "http://127.0.0.1:8888"),
                       TERMINAL_CWD=str(home / "workspace"))
            self.process = await asyncio.create_subprocess_exec(self.settings["python"], "-m", "hermes_cli.main", "gateway", "run",
                cwd=self.settings["repository"], env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(25):
                while True:
                    if self.process.returncode is not None:
                        raise ValueError("Isolated Hermes failed to start")
                    try:
                        await self.request("GET", "/v1/capabilities")
                        break
                    except Exception:
                        await asyncio.sleep(.25)
        capabilities = await self.request("GET", "/v1/capabilities")
        if not all(capabilities.get("features", {}).get(k) for k in ("run_submission", "run_status", "run_stop")):
            raise ValueError("Hermes lacks the voice lifecycle API")

    async def answer(self, text, progress, turn=0):
        """Wait for one finished final answer, never synthesize intermediate tool messages."""
        async with asyncio.timeout(120):
            async with self.lock:
                if self.uncertain:
                    raise ValueError("Previous run outcome is unknown")
                try:
                    self.uncertain = True  # a timeout during submission may have started a tool
                    created = await self.request("POST", "/v1/runs", json={
                        "input": text, "session_id": self.session_id,
                        "model": self.settings["model"], "conversation_history": self.heard_history(),
                        "instructions": self.settings.get("system_prompt", INSTRUCTIONS) + " " + PLAYBACK_INSTRUCTIONS
                            + " Do not repeat tool actions just because an earlier reply is absent from history.",
                        "reasoning_effort": self.settings.get("reasoning_effort", "none")})
                    self.run_id = created["run_id"]
                    while True:
                        state = await self.request("GET", "/v1/runs/" + self.run_id)
                        status = state.get("status")
                        if status == "completed":
                            self.uncertain = False
                            answer = state.get("output", "")
                            self.history.append({"turn": turn, "user": text})
                            self.history[:] = self.history[-40:]
                            return answer
                        if status in ("failed", "cancelled"):
                            self.uncertain = False
                            raise ValueError("Hermes run ended without an answer")
                        if status == "waiting_for_approval":
                            await progress("approval_required")
                            # Voice has no safe approval UI yet: refuse rather than act silently.
                            await self.stop()
                            raise ValueError("Tool approval required")
                        await asyncio.sleep(.2)
                finally:
                    if self.uncertain and self.run_id:
                        await asyncio.shield(self.stop())
                    self.run_id = None

    async def stop(self):
        """Stop once and wait, boundedly, for Hermes to confirm that it let go."""
        if not self.run_id:
            return
        await self.request("POST", "/v1/runs/" + self.run_id + "/stop", json={})
        async with asyncio.timeout(10):
            while True:
                state = await self.request("GET", "/v1/runs/" + self.run_id)
                if state.get("status") in ("completed", "failed", "cancelled"):
                    self.uncertain = False
                    return
                await asyncio.sleep(.2)

    def heard_history(self):
        """Only confirmed words enter assistant history; delivery guidance stays in instructions."""
        result = []
        for item in self.history:
            result.append({"role": "user", "content": item["user"]})
            heard = speakable(" ".join(self.delivery.get(item["turn"], [])))
            if heard:
                result.append({"role": "assistant", "content": heard})
        return result

    async def close(self):
        """Stop only the process created for this call, then remove its temporary home."""
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.home:
            self.home.cleanup()
