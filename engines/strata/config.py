"""Prepared per-node Strata installation, separate from llama.cpp and disabled by default."""
from dataclasses import asdict, dataclass, fields
import os
from pathlib import Path

from engines.strata.model import absolute_path


@dataclass(frozen=True)
class NodeSettings:
    """Validated node-local Strata runtime configuration."""
    enabled: bool = False
    source: str = ""
    python: str = ""
    executable: str = ""
    host_ram_mib: int = 0
    allow_host_ram: bool = False

    @classmethod
    def read(cls, options: dict) -> "NodeSettings":
        """Validate structure only; no subprocess, model load or filesystem mutation."""
        if not isinstance(options, dict) or options.keys() - {field.name for field in fields(cls)}:
            raise ValueError("Strata node settings accept enabled, source, python, executable, host_ram_mib and allow_host_ram only.")
        result = cls(**options)
        if type(result.enabled) is not bool or type(result.allow_host_ram) is not bool:
            raise ValueError("Strata enabled and allow_host_ram must be true or false.")
        if type(result.host_ram_mib) is not int or not 0 <= result.host_ram_mib <= 2**31:
            raise ValueError("Strata host_ram_mib must be a whole number of MiB, 0 or more.")
        for name in ("source", "python", "executable"):
            value = getattr(result, name)
            if not isinstance(value, str) or value or result.enabled:
                absolute_path(value, name)
        if result.enabled and (not result.allow_host_ram or not result.host_ram_mib):
            raise ValueError("Enabling Strata requires explicit allow_host_ram and a positive host RAM budget; llama.cpp's policy is unchanged.")
        return result

    def check_paths(self) -> None:
        """Check availability without executing an interpreter or a native binary."""
        self.read(asdict(self))
        if not self.enabled:
            raise ValueError("Strata is disabled on this node.")
        if not Path(self.source).is_dir():
            raise ValueError("Strata source directory is unavailable.")
        for name in ("python", "executable"):
            path = Path(getattr(self, name))
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"Strata {name} is not an executable file.")
        # Do not resolve python before locating its private environment: venv's interpreter
        # is commonly a symlink to a shared system interpreter.
        venv_config = Path(self.python).parent.parent / "pyvenv.cfg"
        if not venv_config.is_file():
            raise ValueError("Strata requires an explicit private Python virtual environment.")
        options = {}
        for line in venv_config.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                options[key.strip().casefold()] = value.strip().casefold()
        if options.get("include-system-site-packages") != "false":
            raise ValueError("Strata's private environment must not inherit system site packages.")
        if not (Path(self.executable).parent / "libstrata_kq.so").is_file():
            raise ValueError("Strata's matching native libstrata_kq.so is unavailable.")


def settings_from(form: dict[str, list[str]], read) -> dict:
    """Typed model form parser: never forward arbitrary native flags or shell input."""
    one = lambda name: form.get(name, [""])[-1].strip()
    if any(form.get(field) for field in ("flag", "value", "adapter", "projector", "cpu", "allow_split", "parallel_enabled")):
        raise ValueError("Strata does not accept llama.cpp flags, adapters, CPU-only placement or GPU splitting.")
    wanted = list(dict.fromkeys(card.strip() for card in form.get("gpu_card", []) if card.strip()))
    result = {"engine": "strata", "flags": {"model": one("file"), "ctx-size": read(one("ctx"))},
              "strata": {"tokenizer": one("tokenizer")}, "gpus": wanted or "any"}
    if one("min_context"):
        result["min_context"] = read(one("min_context"))
    if one("alone"):
        result["alone"] = True
    return result
