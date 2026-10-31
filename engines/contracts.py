"""Small engine boundary; admission and process ownership stay in LoadCoach."""

from dataclasses import dataclass, field
from types import ModuleType
from typing import Protocol
from collections.abc import Callable


@dataclass(frozen=True)
class Installation:
    """Configured engine installation and its availability metadata."""

    engine: str
    enabled: bool
    executable: str


@dataclass(frozen=True)
class Launch:
    """Prepared child-process command, environment and ownership requirements."""

    command: list[str]
    environment: dict[str, str]
    health_path: str = "/health"
    stop_seconds: float = 10
    process_group: bool = (
        False  # Linux-owned session; descendants must exit before release
    )
    capture_output: bool = True
    memory_mib: int = 0  # opt-in nested cgroup, bounded by the existing worker unit
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    cleanup: Callable[[], None] | None = field(default=None, repr=False)
    build: str = ""


class Engine(Protocol):
    """Interface implemented by each supported inference engine."""

    id: str
    label: str
    model: ModuleType

    def schema(self) -> dict:
        """Return the editable configuration schema for this inference engine."""
        ...

    def installation(self, config) -> Installation:
        """Describe the configured engine executable and whether it is enabled."""
        ...

    def host_ram_budget(self, config, settings: dict) -> int:
        """Return the engine host-memory reservation in MiB."""
        ...

    def launch(
        self, config, port: int, settings: dict, cards: tuple[str, ...]
    ) -> Launch:
        """Prepare the engine command, environment and process ownership contract."""
        ...

    def capabilities(self, settings: dict) -> dict:
        """Describe the request features this engine supports."""
        ...

    def parse_form(self, submitted: dict, read) -> dict:
        """Parse submitted model fields into engine-specific configuration."""
        ...

    def preview(
        self,
        name: str,
        candidate: dict,
        library,
        runner,
        supported: list[dict],
        engine_error: str = "",
    ) -> dict:
        """Validate candidate model settings and report changes before applying them."""
        ...

    def suggestions(
        self, settings: dict, defaults: dict, cards: list[str]
    ) -> list[dict]:
        """Return compatible engine-specific setup suggestions for the model."""
        ...

    def describe(self, config, settings: dict | None = None) -> dict:
        """Return public engine availability and configuration metadata."""
        ...

    def failure_reason(self, output: str) -> str:
        """Classify engine failure output for the operator."""
        ...

    def healthy(self, response) -> bool:
        """Check whether the engine HTTP service is ready to accept requests."""
        ...

    async def version(self, executable: str) -> str:
        """Return the cached executable build identity."""
        ...

    async def supported_flags(self, executable: str, cache: dict) -> list[dict]:
        """Return the configuration flags accepted by this engine installation."""
        ...
