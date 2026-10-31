"""Private launch handoff owned by Runner or an isolated qualification context."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import tempfile

from engines.strata.artifacts import verify
from engines.strata.config import NodeSettings
from engines.strata import model
from engines.strata.runtime import verify_source
from engines.contracts import Launch


@contextmanager
def launch_config(node: NodeSettings, settings: dict, expected: dict, *, gpu: int,
                  port: int, name: str, directory: str, build_manifest: dict | None = None):
    """Prepare one key/config, removed even on cancellation; never start a process.

    Caller retains this context until the whole engine process tree is reaped. A file's
    existence is not a resource reservation or authorization to bypass openWeightGym admission.
    """
    model.check_model(name, settings, tuple(settings.get("gpus", ())) if isinstance(settings.get("gpus"), list) else ())
    if type(gpu) is not int or not 0 <= gpu <= 255 or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Strata launch requires an assigned GPU ordinal and loopback port.")
    model.absolute_path(directory, "private launch directory")
    node.check_paths()
    verify_source(Path(node.source))
    if build_manifest is not None:
        from engines.strata.build import verify as verify_build
        verify_build(node, build_manifest)
    verify(settings, expected)
    with tempfile.TemporaryDirectory(prefix="strata-launch-", dir=directory) as temporary:
        key = secrets.token_urlsafe(36)
        path = Path(temporary) / "launch.json"
        configuration = {"source": node.source, "executable": node.executable,
                         "weights": settings["flags"]["model"], "tokenizer": settings["strata"]["tokenizer"],
                         "context": settings["flags"]["ctx-size"], "gpu": gpu,
                         "host_ram_mib": node.host_ram_mib, "model_name": name,
                         "port": port, "api_key": key, "artifact_manifest": expected}
        if build_manifest is not None:
            configuration["build_manifest"] = build_manifest
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(configuration, stream)
        yield path, key


@contextmanager
def prepared_launch(node: NodeSettings, settings: dict, artifacts: dict, build_manifest: dict, *,
                    gpu: int, port: int, name: str, directory: str):
    """A complete bounded launch specification, not permission to bypass admission.

    The caller must retain this context until Runner has reaped the group and closed its
    memory cgroup. Preparing files neither starts a process nor changes any service limit.
    """
    from engines.strata.build import python_command
    from engines.strata import runtime
    if not isinstance(build_manifest, dict):
        raise ValueError("A bounded Strata launch requires a reviewed build manifest.")
    with launch_config(node, settings, artifacts, gpu=gpu, port=port, name=name, directory=directory,
                       build_manifest=build_manifest) as (path, key):
        command = python_command(node.python, str(Path(runtime.__file__).resolve()), str(path.parent), "--config", str(path))
        yield Launch(command, runtime.child_environment(gpu), process_group=True, capture_output=False,
                     memory_mib=node.host_ram_mib, headers={"Authorization": "Bearer "+key})
