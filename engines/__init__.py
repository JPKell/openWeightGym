"""Registry of implemented inference engines. Missing selection means legacy llama.cpp."""
from engines.llama_cpp.adapter import LlamaCpp
from engines.strata.adapter import Strata
from mirrorwall.engine import SDCpp
from soundroom.engine import AudioCpp
from engines.whisper_cpp.adapter import WhisperCpp

DEFAULT = "llama_cpp"
_ENGINES = {DEFAULT: LlamaCpp(), "strata": Strata(), "sdcpp": SDCpp(), "audiocpp": AudioCpp(), "whispercpp": WhisperCpp()}


def engine_ids():
    """List registered engines without inspecting their installed runtimes."""
    return tuple(_ENGINES)


def engine_for(settings=None):
    """Resolve the configured engine, preserving the default llama.cpp selection."""
    name = settings.get("engine", DEFAULT) if isinstance(settings, dict) else settings or DEFAULT
    if not isinstance(name, str) or name not in _ENGINES:
        raise ValueError(f"Unknown inference engine {name!r}. Available: {', '.join(_ENGINES)}.")
    return _ENGINES[name]


def catalogue(config) -> list[dict]:
    """Return the static registry of supported inference engine adapters."""
    return [engine.describe(config) for engine in _ENGINES.values()]


def checked_installations(engines, legacy: str) -> None:
    """Validate paths/settings only. This never invokes a native program."""
    if not isinstance(engines, dict):
        raise ValueError("engines must be a table of engine settings")
    if not isinstance(legacy, str) or not legacy.strip() or "\x00" in legacy:
        raise ValueError("llama_server must name an executable")
    for name, options in engines.items():
        engine_for(name)
        if name == 'whispercpp':
            from types import SimpleNamespace
            from engines.whisper_cpp.adapter import node_options as whisper_options
            whisper_options(SimpleNamespace(engines=engines))
            continue
        if name == "sdcpp":
            from types import SimpleNamespace
            from mirrorwall.engine import node_options
            node_options(SimpleNamespace(engines=engines))
            continue
        if name == "audiocpp":
            from types import SimpleNamespace
            from soundroom.engine import node_options as audio_options
            audio_options(SimpleNamespace(engines=engines))
            continue
        if name == "strata":
            from engines.strata.config import NodeSettings
            NodeSettings.read(options)
            continue
        if not isinstance(options, dict) or options.keys() - {"enabled", "executable"}:
            raise ValueError(f"engines.{name} accepts enabled and executable")
        if type(options.get("enabled", True)) is not bool:
            raise ValueError(f"engines.{name}.enabled must be true or false")
        executable = options.get("executable", legacy)
        if not isinstance(executable, str) or not executable.strip() or "\x00" in executable:
            raise ValueError(f"engines.{name}.executable must name an executable")

