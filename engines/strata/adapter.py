"""Opt-in Strata integration; openWeightGym retains admission, credentials and process ownership."""
import asyncio
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
from pathlib import Path
import re
import tempfile

from engines.contracts import Installation
from engines.strata import artifacts, build, model
from engines.strata.config import NodeSettings, settings_from
from engines.strata.prepare import prepared_launch
from loadcoach.engine_memory import delegated_parent


class Strata:
    """Strata model configuration, controlled runtime and resource adapter."""
    id, label, model = "strata", "Strata", model

    def node(self, config):
        """Read the node's validated Strata installation settings."""
        return NodeSettings.read(getattr(config, "engines", {}).get(self.id, {}))

    def installation(self, config):
        """Describe the configured engine executable and whether it is enabled."""
        node = self.node(config)
        return Installation(self.id, node.enabled, node.executable)

    def schema(self):
        """Return the editable configuration schema for this inference engine."""
        return json.loads(Path(__file__).with_name("schema.json").read_text())

    def capabilities(self, settings):
        """Describe the request features this engine supports."""
        return {"version": 1, **self.schema()["capabilities"]}

    def describe(self, config, settings=None):
        """Return public engine availability and configuration metadata."""
        node = self.node(config)
        why = ""
        try:
            node.check_paths()
            delegated_parent()
        except (ValueError, OSError) as problem:
            why = str(problem)
        return {"id": self.id, "label": self.label, "enabled": node.enabled,
                "executable": node.executable, "settings": asdict(node), "available": not why,
                "why": why, "schema": self.schema(), "capabilities": self.capabilities(settings or {})}

    def host_ram_budget(self, config, settings):
        """Return the engine host-memory reservation in MiB."""
        node = self.node(config)
        return node.host_ram_mib if node.enabled else 0

    def check_available(self, config, settings):
        """Reject an unavailable or incompatible Strata runtime installation."""
        node = self.node(config)
        node.check_paths()
        parent = delegated_parent()
        if node.host_ram_mib * 2**20 > int((parent / "memory.max").read_text()):
            raise ValueError("Strata RAM budget exceeds this worker's existing memory ceiling.")
        if any(not Path(path).is_file() for path in model.files_of(settings).values()):
            raise ValueError("Strata requires prepared weights and all four explicit tokenizer files on this node.")

    def launch(self, config, port, settings, cards, *, name="suite-strata"):
        """Blocking preparation runs off-loop; the returned cleanup stays owned by Runner."""
        node = self.node(config)
        node.check_paths()
        delegated_parent()  # fail before expensive hashing or creating any launch files
        if len(cards) != 1 or not re.fullmatch(r"CUDA[0-9]+", cards[0]):
            raise ValueError("Strata requires exactly one assigned CUDA GPU.")
        pinned_build = build.manifest(node)
        pinned_artifacts = artifacts.manifest(settings)
        stack = ExitStack()
        try:
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="suite-strata-"))
            launch = stack.enter_context(prepared_launch(node, settings, pinned_artifacts, pinned_build,
                gpu=int(cards[0][4:]), port=port, name=name, directory=directory))
            return replace(launch, cleanup=stack.close, build=pinned_build["sha256"])
        except BaseException:
            stack.close()
            raise

    async def build_version(self, config):
        """Read the qualified Strata build identity from its manifest."""
        return (await asyncio.to_thread(build.manifest, self.node(config)))["sha256"]

    async def supported_flags(self, executable, cache):
        """Return the configuration flags accepted by this engine installation."""
        return []  # typed fields only; never execute arbitrary --help

    def parse_form(self, submitted, read):
        """Parse submitted model fields into engine-specific configuration."""
        return settings_from(submitted, read)

    def preview(self, name, candidate, library, runner, supported, engine_error=""):
        """Validate candidate model settings and report changes before applying them."""
        from engines.strata.setup import preview
        return preview(name, candidate, library, runner)

    def suggestions(self, settings, defaults, cards):
        """Return compatible engine-specific setup suggestions for the model."""
        return []

    def healthy(self, response):
        """Check whether the engine HTTP service is ready to accept requests."""
        return response.status_code == 200

    def failure_reason(self, output):
        """Classify engine failure output for the operator."""
        return "Strata stopped; check prepared artifacts, delegated memory and the pinned installation."

    def check_request(self, path, asked):
        """Reject request features outside the qualified Strata contract."""
        self.prepare_request(path, asked)

    def prepare_request(self, path, asked):
        """Normalize an accepted request for the controlled Strata runtime."""
        from engines.strata.runtime import checked_request
        if path != "/v1/chat/completions":
            raise ValueError("Strata supports text chat only; native completion, tokenization and embeddings are unsupported.")
        sent = dict(asked)
        if "chat_template_kwargs" in sent:
            if sent.pop("chat_template_kwargs") != {"enable_thinking": False}:
                raise ValueError("Strata only supports disabled thinking, not arbitrary chat-template options.")
        if "max_tokens" not in sent and "max_completion_tokens" not in sent:
            sent["max_tokens"] = 256  # bounded default for ordinary openWeightGym/web-chat callers
        return checked_request(sent)
