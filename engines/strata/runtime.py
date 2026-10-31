"""Constrained text-only bridge to Strata. LoadCoach remains the lifecycle owner.

The opt-in adapter uses this bridge without running upstream setup, MCP, UI or automatic
residency. It is also executable for bounded isolated qualification.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

if __package__ in (None, ""):
    # The bounded launcher uses an absolute script and isolated interpreter, never PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loadcoach.host_memory import reclaimable_memory_charge as startup_memory_charge

REVISION = "477ac13cbe8e582c20484871ce459d20d82df09d"
SERVER_SHA256 = "9eb13d12dc3ed0f6c94d20ac1c0018efe1e00c331deae76bd0fb9f420e005232"
MIB = 2**20


def verify_source(source: Path) -> None:
    """Only import the reviewed, unchanged checkout; never fetch or execute setup here."""
    revision = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    changed = subprocess.run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
                             check=True, capture_output=True, text=True).stdout
    digest = hashlib.sha256((source / "serve/server.py").read_bytes()).hexdigest()
    if revision != REVISION or changed or digest != SERVER_SHA256:
        raise ValueError("Strata requires the unchanged reviewed source revision.")


def child_environment(gpu: int) -> dict[str, str]:
    """No inherited debug, hooks, Python startup paths or other engine tuning."""
    kept = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR") if key in os.environ}
    return {**kept, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(gpu),
            "PYTHONDONTWRITEBYTECODE": "1", "GGML_CUDA_ENABLE_UNIFIED_MEMORY": "0"}


def require_ram(expert_bytes: int, budget_mib: int, available_mib: int, headroom_mib: int = 512) -> int:
    """Qwen36 keeps all routed experts in host RAM, even those cached in VRAM.

    Add a conservative 2 GiB for the wrapper, dense-weight staging and runtime. This is
    preflight, not a measurement or an OS limit: qualification must also use a capped cgroup.
    """
    if any(type(value) is not int or value < 0 for value in (expert_bytes, budget_mib, available_mib, headroom_mib)) or not expert_bytes:
        raise ValueError("Strata RAM estimates and budgets must be positive whole-byte/MiB values.")
    required = (expert_bytes + MIB - 1) // MIB + 2048
    if required > budget_mib or required > available_mib - headroom_mib:
        raise ValueError(f"Strata needs at least {required} MiB of host RAM plus {headroom_mib} MiB headroom; "
                         f"budget is {budget_mib} MiB and available RAM is {available_mib} MiB. Model not loaded.")
    return required


def require_memory_scope(budget_mib: int, required_mib: int) -> None:
    """A JSON budget is not an OS limit. Require bounded, no-swap cgroup-v2 execution."""
    group = next((line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")), None)
    if group is None:
        raise ValueError("Strata qualification requires a capped cgroup-v2 memory scope.")
    folder = Path("/sys/fs/cgroup") / group.lstrip("/")
    limit = (folder / "memory.max").read_text().strip()
    swap = (folder / "memory.swap.max").read_text().strip()
    current = int((folder / "memory.current").read_text())
    try:
        statistics = (folder / "memory.stat").read_text()
    except OSError:
        statistics = ""  # unknown cache is charged in full
    current = max(current, int((folder / "memory.current").read_text()))
    charged = startup_memory_charge(current, statistics)
    if limit == "max" or swap != "0" or int(limit) > budget_mib * MIB or int(limit) - charged < required_mib * MIB:
        raise ValueError("Strata qualification needs sufficient cgroup room, MemoryMax within its RAM budget, and MemorySwapMax=0.")


def checked_request(request: dict) -> dict:
    """Refuse unqualified operations before rendering, fetching files or native generation."""
    allowed = {"model", "messages", "max_tokens", "max_completion_tokens", "stream", "stream_options",
               "temperature", "top_p", "top_k", "min_p", "seed", "stop", "reasoning_effort"}
    if not isinstance(request, dict) or request.keys() - allowed:
        raise ValueError("This Strata trial supports text chat and basic sampling only.")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(item, dict) or item.keys() - {"role", "content"}
        or item.get("role") not in ("system", "user", "assistant") or not isinstance(item.get("content"), str)
        for item in messages
    ):
        raise ValueError("Strata trial messages require system/user/assistant roles and text content.")
    caps = [request[key] for key in ("max_tokens", "max_completion_tokens") if key in request]
    if len(caps) != 1 or type(caps[0]) is not int or not 1 <= caps[0] <= 8192:
        raise ValueError("Set one integer max_tokens or max_completion_tokens limit from 1 through 8192.")
    if request.get("reasoning_effort", "none") != "none":
        raise ValueError("Reasoning is not qualified for the initial Strata trial.")
    for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1), ("min_p", 0, 1)):
        if key in request and (type(request[key]) not in (int, float) or not math.isfinite(request[key]) or not low <= request[key] <= high):
            raise ValueError(f"Strata {key} must be a finite number from {low} through {high}.")
    if "top_k" in request and (type(request["top_k"]) is not int or not 1 <= request["top_k"] <= 64):
        raise ValueError("Strata top_k must be a whole number from 1 through 64; values are never silently clamped.")
    if "seed" in request and (type(request["seed"]) is not int or not 1 <= request["seed"] < 2**31):
        raise ValueError("Strata seed must be a positive 31-bit integer.")
    if "stop" in request and not (isinstance(request["stop"], str) and request["stop"] or
            isinstance(request["stop"], list) and 1 <= len(request["stop"]) <= 4 and
            all(isinstance(item, str) and item for item in request["stop"])):
        raise ValueError("Strata stop must be nonempty text or one to four nonempty strings.")
    if "stream" in request and type(request["stream"]) is not bool:
        raise ValueError("stream must be true or false.")
    options = request.get("stream_options", {})
    if not isinstance(options, dict) or options.keys() - {"include_usage"} or (
        "include_usage" in options and type(options["include_usage"]) is not bool
    ):
        raise ValueError("stream_options accepts include_usage true or false only.")
    return {**request, "reasoning_effort": "none"}


def controlled_classes(upstream):
    """Reuse reviewed parsing, but prevent hidden model reloads and generation replay."""
    upstream.START_RETRIES = 0

    class OwnedEngine(upstream.StrataEngine):
        """Engine wrapper retaining child-process ownership during generation."""
        def restart(self, *args, **kwargs):
            """Restart the controlled engine while retaining process ownership."""
            raise upstream.EngineDied("Strata ended; openWeightGym must admit a new model load.")

        def generate(self, *args, **kwargs):
            """Generate through the controlled engine without releasing its resource owner."""
            try:
                yield from super().generate(*args, **kwargs)
            except upstream.EngineNonFinite:
                # Service catches EngineNonFinite and replays; EngineDied fails the same
                # request instead, including before the first token. Never replay it here.
                raise upstream.EngineDied("Strata produced non-finite output; request not replayed.") from None

    class OwnedService(upstream.Service):
        """Runtime service that preserves controlled engine lifecycle requirements."""
        def ensure_loaded(self):
            """Ensure the requested model is loaded in the owned engine."""
            if not self.loaded():
                raise upstream.EngineDied("Strata ended; openWeightGym must admit a new model load.")

        def _say_died(self, error):
            # Upstream's diagnostic promises an automatic restart and reads native logs.
            # Neither applies to this bridge; the response already carries the failure.
            """Record a controlled engine failure for the runtime service."""
            pass

    return OwnedEngine, OwnedService


def http_server(upstream, service, port: int, api_key: str):
    """Loopback API only, including authenticated health. No upstream browser/admin routes."""
    parent = upstream.make_handler(service)

    class Handler(parent):
        """HTTP handler for the bounded service protocol."""
        def permitted(self, paths):
            """Check the private runtime request's authorization token."""
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + api_key):
                self.close_connection = True
                self._json(401, {"error": {"message": "Authentication required."}})
                return False
            if self.path not in paths:
                self.close_connection = True
                self._json(404, {"error": {"message": "Unsupported Strata trial operation."}})
                return False
            return True

        def do_GET(self):
            """Serve supported private runtime health and metadata requests."""
            if self.permitted({"/health", "/v1/models"}):
                super().do_GET()

        def do_POST(self):
            """Read and dispatch an authenticated runtime inference request."""
            if self.permitted({"/v1/chat/completions"}):
                super().do_POST()

        def _openai(self, request):
            """Convert the runtime result into the supported OpenAI response format."""
            return super()._openai(checked_request(request))

    service.api_key = api_key
    service.answer_room = False
    service.repeat_stop_tokens = 0
    server = upstream.Server(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.svc = service
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(config: dict) -> None:
    """Start the controlled runtime with the validated model and memory contract."""
    allowed = {"source", "executable", "weights", "tokenizer", "context", "gpu", "host_ram_mib", "model_name", "port", "api_key"}
    if not isinstance(config, dict) or config.keys() - {"artifact_manifest", "build_manifest"} != allowed:
        raise ValueError("Strata launch requires the exact typed trial configuration.")
    for key in ("source", "executable", "weights", "tokenizer", "model_name", "api_key"):
        if not isinstance(config[key], str) or not config[key] or "\0" in config[key]:
            raise ValueError("Strata launch paths, name and private key must be nonempty strings.")
    for key, low, high in (("context", 256, 8192), ("gpu", 0, 255), ("host_ram_mib", 1, 2**31), ("port", 1, 65535)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"Invalid Strata trial {key}.")
    if len(config["api_key"]) < 32:
        raise ValueError("Strata requires a private launch key of at least 32 characters.")
    if "build_manifest" in config:
        from engines.strata.build import verify as verify_build
        from engines.strata.config import NodeSettings
        verify_build(NodeSettings.read({"enabled": True, "allow_host_ram": True, "python": sys.executable,
            "source": config["source"], "executable": config["executable"], "host_ram_mib": config["host_ram_mib"]}), config["build_manifest"])
    if "artifact_manifest" in config:
        from engines.strata.artifacts import verify
        verify({"engine": "strata", "flags": {"model": config["weights"], "ctx-size": config["context"]},
                "strata": {"tokenizer": config["tokenizer"]}}, config["artifact_manifest"])
    source = Path(config["source"]).resolve()
    verify_source(source)
    environment = child_environment(config["gpu"])
    os.environ.clear()
    os.environ.update(environment)
    sys.path[:0] = [str(source), str(source / "tools")]
    from gguf_reader import GGUFFile
    from strata_tokenizer import Tokenizer
    from serve import server as upstream
    from serve.frontend import ChatTemplate

    gguf = GGUFFile(Path(config["weights"]))
    if gguf.metadata.get("general.architecture") != "qwen35moe":
        raise ValueError("The initial Strata trial supports qwen35moe only.")
    experts = [tensor.expected_bytes() for tensor in gguf.tensors if "_exps.weight" in tensor.name]
    if not experts or any(size is None for size in experts):
        raise ValueError("Cannot safely estimate this Strata model's host expert allocation.")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    required = require_ram(sum(experts), config["host_ram_mib"], int(memory["MemAvailable"].split()[0]) // 1024)
    require_memory_scope(config["host_ram_mib"], required)
    tokenizer = Path(config["tokenizer"])
    vocab = json.loads((tokenizer / "vocab.json").read_text())
    tokens = [None] * len(vocab)
    for token, index in vocab.items():
        tokens[index] = token
    tok = Tokenizer(tokens, (tokenizer / "merges.txt").read_text().split("\n"),
                    json.loads((tokenizer / "token_type.json").read_text()))
    template = ChatTemplate(tokenizer / "chat_template.jinja")
    Engine, Service = controlled_classes(upstream)
    # No automatic warmup request: qualify cold start and the first real request separately.
    args = ["--native", config["weights"], "--max-context", str(config["context"]), "--prefill", "512",
            "--mtp", "0", "--cpu-threads", "2", "--no-warmup"]
    engine = Engine(config["executable"], args, cwd=str(source), env=child_environment(config["gpu"]))
    httpd = None
    try:
        service = Service(engine, tok, template, model_name=config["model_name"])
        httpd = http_server(upstream, service, config["port"], config["api_key"])
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        stopped.wait()
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        engine.close()


def main():
    """Parse controlled worker arguments and start the private runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    path = Path(args.config)
    if path.stat().st_mode & 0o077:
        parser.error("Strata launch configuration must be owner-only.")
    config = json.loads(path.read_text())
    # Upstream diagnostics can contain text. Nothing from this private wrapper/native
    # process is persisted; health and request responses carry the operator-visible state.
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        try:
            run(config)
        except Exception:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
