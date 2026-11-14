"""llama.cpp command, environment, health and executable metadata contracts."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx

from engines.contracts import Installation, Launch
from engines.llama_cpp import model


def command_line(llama_server: str, port: int, flags: dict, cards: tuple[str, ...], adapters: dict | None = None) -> list[str]:
    """Preserve the existing ordered llama-server argv, including LoRA scales."""
    command = [llama_server, "--host", "127.0.0.1", "--port", str(port)]
    command += ["--device", ",".join(cards) or "none"]
    if adapters:
        command += ["--lora-scaled", ",".join(f"{adapter['file']}:{float(adapter.get('scale', 1))}" for adapter in adapters.values())]
    for flag, value in flags.items():
        if value is False:
            continue
        command.append(f"--{flag}")
        if value is not True:
            command.append(",".join(map(str, value)) if isinstance(value, list) else str(value))
    return command


def server_environment(config, adapters: dict | None = None) -> dict[str, str]:
    """Build the child environment for the selected llama.cpp device placement."""
    return {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            "GGML_CUDA_ENABLE_UNIFIED_MEMORY": "0" if config.disable_ram_offload else "1"} \
           | ({"GGML_CUDA_DISABLE_GRAPHS": "1"} if adapters else {})


def engine_stamp(executable: str) -> list | None:
    """Stat the same resolved program execution uses; never start it to inspect its path."""
    path = os.path.realpath(shutil.which(executable) or executable)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


class LlamaCpp:
    """llama.cpp model configuration, process launch and health adapter."""
    id, label, model = "llama_cpp", "llama.cpp", model

    def host_ram_budget(self, config, settings: dict) -> int:
        # Existing CPU admission uses RSS; GPU models retain their existing VRAM policy.
        """Return the engine host-memory reservation in MiB."""
        return 0

    def parse_form(self, submitted: dict, read) -> dict:
        """Parse submitted model fields into engine-specific configuration."""
        from engines.llama_cpp.form import settings_from
        return settings_from(submitted, read)

    def preview(self, name: str, candidate: dict, library, runner, supported: list[dict], engine_error: str = "") -> dict:
        """Validate candidate model settings and report changes before applying them."""
        from engines.llama_cpp.setup import preview
        return preview(name, candidate, library, runner, supported, engine_error)

    def suggestions(self, settings: dict, defaults: dict, cards: list[str]) -> list[dict]:
        """Return compatible engine-specific setup suggestions for the model."""
        from engines.llama_cpp.form import suggested
        return suggested(settings, defaults, cards)

    def schema(self) -> dict:
        """Return the editable configuration schema for this inference engine."""
        return json.loads(Path(__file__).with_name("schema.json").read_text())

    def installation(self, config) -> Installation:
        """Describe the configured engine executable and whether it is enabled."""
        options = getattr(config, "engines", {}).get(self.id, {})
        return Installation(self.id, options.get("enabled", True), options.get("executable", config.llama_server))

    def launch(self, config, port: int, settings: dict, cards: tuple[str, ...]) -> Launch:
        """Prepare the engine command, environment and process ownership contract."""
        installed = self.installation(config)
        if not installed.enabled:
            raise ValueError("llama.cpp is disabled on this compute node.")
        from loadcoach.devices import cuda_environment
        details = {one['id']: one for one in config.gpu_details}
        around = server_environment(config, settings.get('adapters'))
        if all(card.startswith('CUDA') for card in cards):
            around['CUDA_VISIBLE_DEVICES'] = cuda_environment(config, cards)['CUDA_VISIBLE_DEVICES']
            devices = tuple(f'CUDA{index}' for index in range(len(cards)))
        else:
            devices = tuple(details.get(card, {}).get('llama_device', card) for card in cards)
        return Launch(command_line(installed.executable, port, settings['flags'], devices, settings.get('adapters')), around,
                      health_path='/v1/models' if self.capabilities(settings).get('classifier') else '/health')

    def failure_reason(self, output: str) -> str:
        """Classify engine failure output for the operator."""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        marked = [line.split(" E ", 1)[1].strip() for line in lines if " E " in line]
        plain = [line for line in lines if line.lower().startswith("error")]
        return (marked or plain or lines[-1:] or ["it printed nothing"])[0][:300]

    def healthy(self, response) -> bool:
        """Check whether the engine HTTP service is ready to accept requests."""
        if response.request.url.path == '/v1/models':
            try:
                return response.status_code == 200 and any('decisions' in item.get('architecture', {}).get('output_modalities', [])
                    for item in response.json().get('data', []))
            except (ValueError, AttributeError, TypeError):
                return False
        return response.status_code == 200

    def capabilities(self, settings: dict) -> dict:
        """Describe the request features this engine supports."""
        from loadcoach.classifier import model_facts, limits
        decision = model_facts(settings)
        if decision:
            return {'version': 1, 'classifier': decision['decision_type'], 'chat': False, 'streaming': False,
                    'embeddings': False, 'raw_completion': False, 'prompt_rendering': False, 'tools': False,
                    'tokenize': True, 'images': bool(settings.get('flags', {}).get('mmproj')),
                    'json_schema': True, 'adapters': False, 'native_probes': True, 'limits': limits(decision)}
        embedding = bool(settings.get("flags", {}).get("embedding"))
        return {"version": 1, "chat": not embedding, "streaming": not embedding,
                "embeddings": embedding, "tokenize": True, "raw_completion": not embedding,
                "prompt_rendering": not embedding, "images": bool(settings.get("flags", {}).get("mmproj")),
                "json_schema": not embedding,
                "adapters": True, "native_probes": True}

    async def runtime_metadata(self, client: httpx.AsyncClient, server) -> dict:
        """Read template capabilities once after loading, without starting any process.

        Older engines omit these fields. Keep that distinct from a reported empty
        level list, and never expose paths or templates from the rest of /props.
        """
        if client is None:
            return {}
        try:
            response = await client.get(server.url + "/props", headers=server.headers, timeout=2)
            response.raise_for_status()
            props = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        caps = props.get("chat_template_caps") if isinstance(props, dict) else None
        if not isinstance(caps, dict):
            return {}
        found = {key: value for key, value in caps.items() if isinstance(key, str) and type(value) is bool}
        levels = caps.get("reasoning_effort_levels")
        if isinstance(levels, list) and all(isinstance(level, str) and level for level in levels):
            found["reasoning_effort_levels"] = list(levels)
        return {"chat_template_caps": found} if found else {}

    def describe(self, config, settings: dict | None = None) -> dict:
        """Return public engine availability and configuration metadata."""
        installed = self.installation(config)
        path = shutil.which(installed.executable)
        available = bool(path and os.access(path, os.X_OK))
        return {"id": self.id, "label": self.label, "enabled": installed.enabled,
                "executable": installed.executable, "available": available,
                "why": "Disabled on this node." if not installed.enabled else "" if available else "Executable is unavailable on this node.",
                "schema": self.schema(), "capabilities": self.capabilities(settings or {})}

    async def version(self, executable: str) -> str:
        """Return the cached executable build identity."""
        asked = None
        try:
            asked = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            printed, _ = await asyncio.wait_for(asked.communicate(), 15)
            return next((line.removeprefix("version:").strip() for line in printed.decode(errors="replace").splitlines() if line.startswith("version:")), "")
        except (OSError, TimeoutError):
            return ""
        finally:
            if asked is not None and asked.returncode is None:
                asked.kill()
                await asked.wait()

    async def supported_flags(self, executable: str, cache: dict) -> list[dict]:
        """Return the configuration flags accepted by this engine installation."""
        key = (self.id, executable)
        stamp = engine_stamp(executable)
        if key in cache and cache[key][0] == stamp:
            return cache[key][1]
        asked = None
        try:
            asked = await asyncio.create_subprocess_exec(executable, "--help", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            printed, _ = await asyncio.wait_for(asked.communicate(), 15)
            if asked.returncode:
                raise ValueError(f"{executable} --help exited with status {asked.returncode}.")
            found = model.flags_in(printed.decode(errors="replace"))
            cache[key] = (stamp, found)
            return found
        except (OSError, TimeoutError) as problem:
            raise ValueError(f"{executable} --help could not be run: {problem}") from None
        finally:
            if asked is not None and asked.returncode is None:
                asked.kill()
                await asked.wait()
