"""audio.cpp engine adapter; LoadCoach owns its entire supervised session."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from engines.contracts import Installation, Launch
from loadcoach.engine_memory import delegated_parent
from loadcoach.devices import cuda_environment, identity
from soundroom import model
from soundroom.requests import GENERATION_FIELDS, SCRIPT_FIELDS, SPEECH_FIELDS, TRANSCRIPTION_FIELDS

GENERATIONS, SPEECH, TRANSCRIPTIONS = "/v1/audio/generations", "/v1/audio/speech", "/v1/audio/transcriptions"
FIELDS = {GENERATIONS: GENERATION_FIELDS | SCRIPT_FIELDS, SPEECH: SPEECH_FIELDS, TRANSCRIPTIONS: TRANSCRIPTION_FIELDS,
          "/v1/audio/diarizations": TRANSCRIPTION_FIELDS}


def node_options(config):
    """Validate the optional installation without invoking an executable."""
    options = getattr(config, "engines", {}).get("audiocpp", {})
    if not isinstance(options, dict) or options.keys() - {"enabled", "executable", "cli", "manifest"}:
        raise ValueError("engines.audiocpp accepts enabled, executable, cli and manifest.")
    if type(options.get("enabled", False)) is not bool:
        raise ValueError("engines.audiocpp.enabled must be true or false.")
    for key in ("executable", "cli", "manifest"):
        if key in options and (not isinstance(options[key], str) or not options[key] or "\0" in options[key]):
            raise ValueError("Invalid audio.cpp installation path.")
    executable = options.get("executable", "audiocpp_server")
    return {"enabled": options.get("enabled", False), "executable": executable,
            "cli": options.get("cli", str(Path(executable).with_name("audiocpp_cli"))), "manifest": options.get("manifest", "")}


class AudioCpp:
    """Optional audio configuration, resource admission and native process boundary."""
    id, label, model = "audiocpp", "audio.cpp", model

    def installation(self, config):
        """Describe the explicitly enabled native runtime."""
        options = node_options(config)
        return Installation(self.id, options["enabled"], options["executable"])

    def schema(self):
        """Return audio-specific settings and request capabilities."""
        return {"version": 1, "id": self.id, "label": self.label, "configuration_key": "audiocpp",
                "schema_version": 1, "selectable": False,
                "node": [{"name": "enabled", "type": "boolean", "label": "Enabled", "default": False},
                         {"name": "executable", "type": "string", "label": "audiocpp_server executable", "default": "audiocpp_server"},
                         {"name": "cli", "type": "string", "label": "audiocpp_cli executable", "default": "audiocpp_cli"},
                         {"name": "manifest", "type": "string", "label": "Installation manifest", "default": ""}],
                "family_source": "installed runtime or embedded model contract", "kinds": list(model.TASKS),
                "capabilities": self.capabilities({}), "limits": model.limits({})}

    def capabilities(self, settings):
        """Keep audio work distinct from chat, embeddings and image capabilities."""
        kind = model.kind(settings)
        return {"version": 1, "chat": False, "streaming": False, "embeddings": False, "tokenize": False,
                "raw_completion": False, "prompt_rendering": False, "images": False, "tools": False,
                "image_generation": False, "native_probes": False, "load_probes": True,
                "audio": kind or True, **{name: kind == name for name in model.TASKS}}

    def describe(self, config, settings=None):
        """Expose availability and the audio configuration schema."""
        options = node_options(config)
        found = shutil.which(options["executable"])
        available = bool(found and os.access(found, os.X_OK)) and options["enabled"]
        return {"id": self.id, "label": self.label, **options, "available": available,
                "why": "" if available else "Optional audio.cpp support is not installed/enabled. See Installation → Optional audio runtime.",
                "settings": options, "schema": self.schema(), "capabilities": self.capabilities(settings or {})}

    def host_ram_budget(self, config, settings):
        """Reserve the profile's explicit host RAM cap."""
        return settings.get("audiocpp", {}).get("host_ram_mib", 8192)

    def check_available(self, config, settings):
        """Reject missing files, undeclared native options or no delegated RAM ceiling before admission."""
        options = node_options(config)
        if not options["enabled"] or not shutil.which(options["executable"]):
            raise ValueError("audio.cpp is not installed/enabled on this node; run scripts/install-audiocpp.sh.")
        for label, path in model.files_of(settings).items():
            if not Path(path).is_file():
                raise ValueError("Missing audio package file: " + label)
        model.check_options(settings, options["manifest"])
        delegated_parent()

    def launch(self, config, port, settings, cards):
        """Start a private audio child inside Runner's owned cgroup/session."""
        self.check_available(config, settings)
        options = node_options(config)
        if len(cards) != (1 if settings.get("gpus", 1) else 0):
            raise ValueError("The admitted GPU count does not match the audio profile.")
        directory = tempfile.TemporaryDirectory(prefix="suite-audio-engine-")
        try:
            token = secrets.token_urlsafe(32)
            path = Path(directory.name) / "launch.json"
            path.write_text(json.dumps({"port": port, "token": token, "settings": settings, "installation": options, "cards": list(cards),
                                        'monitor_cards': [identity(config, card) if identity(config, card).startswith('GPU-') else card for card in cards]}))
            path.chmod(0o600)
            # A CPU profile sees no card at all, so it cannot fall back onto one either.
            around = {**cuda_environment(config, cards),
                      "GGML_CUDA_ENABLE_UNIFIED_MEMORY": "0", "OMP_NUM_THREADS": str(settings["audiocpp"].get("threads", 4))}
            executable = Path(shutil.which(options["executable"])).resolve()
            with executable.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            return Launch([sys.executable, "-I", "-B", str(Path(__file__).with_name("runtime.py")), str(path)], around,
                          process_group=True, memory_mib=self.host_ram_budget(config, settings),
                          headers={"authorization": "Bearer " + token}, cleanup=directory.cleanup,
                          build="audiocpp:" + checksum, stop_seconds=20)
        except BaseException:
            directory.cleanup()
            raise

    async def version(self, executable):
        """Fingerprint the configured binary without loading a model."""
        path = shutil.which(executable)
        if not path:
            return ""

        def checksum():
            """Hash the executable on a worker thread."""
            with open(path, "rb") as stream:
                return "audiocpp:" + hashlib.file_digest(stream, "sha256").hexdigest()
        return await asyncio.to_thread(checksum)

    async def supported_flags(self, executable, cache):
        """Audio profiles use typed fields rather than arbitrary flags."""
        return []

    def check_request(self, path, asked):
        """Refuse chat, image and tokenization routes before an audio model is loaded.

        Without settings, any audio route passes here; the admitted profile's kind is checked by its child.
        """
        if path not in FIELDS:
            raise ValueError("This model supports audio routes only (" + ", ".join(sorted(FIELDS)) + ").")
        if not isinstance(asked, dict) or asked.keys() - FIELDS[path]:
            raise ValueError("Unknown audio request field(s).")

    def prepare_request(self, path, asked):
        """Limits and family checks run in the child, against the admitted profile."""
        self.check_request(path, asked)
        return dict(asked)

    def healthy(self, response):
        """The child answers only once its native model is loaded."""
        return response.status_code == 200

    def failure_reason(self, output):
        """Return audio.cpp's own last error line, without prompts or lyrics."""
        lines = [line for line in output.splitlines() if "audio.cpp failed to load" in line or "Error" in line]
        return lines[-1][-500:] if lines else "Audio runtime stopped; check the package files and the GPU/RAM budget."

    def parse_form(self, submitted, read):
        """Parse the audio profile form: typed fields plus native options as JSON."""
        raw = read("audio_options", "{}")
        try:
            options = json.loads(raw)
        except ValueError:
            raise ValueError("Audio options must be a JSON object.") from None
        if not isinstance(options, dict):
            raise ValueError("Audio options must be an object.")
        gpus = read("audio_gpus", "1")
        return {"engine": self.id, "flags": {"model": read("model", ""), "ctx-size": 0}, "audiocpp": options,
                "gpus": int(gpus) if gpus.isdigit() else [gpus], "alone": True}

    def preview(self, name, candidate, library, runner, supported, engine_error=""):
        """Validate typed audio settings using the same contract as execution."""
        errors = []
        try:
            model.check_model(name, candidate, runner.engine_cards(candidate))
        except (ValueError, TypeError) as problem:
            errors.append(str(problem))
        if engine_error:
            errors.append(engine_error)
        return {"errors": errors, "warnings": [], "settings": candidate, "name": name, "changes": [], "engine": self.id}

    def suggestions(self, settings, defaults, cards):
        """Audio profiles have no chat-template guesses."""
        return []
