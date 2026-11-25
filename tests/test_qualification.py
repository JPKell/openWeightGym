"""Image/video qualification evidence: its identity, carry-over, staleness and automatic re-qualification."""
import asyncio
import base64
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from engines import engine_for
from loadcoach.config import Config
from loadcoach.models import Models
from loadcoach.qualification import Qualifier
from loadcoach.runner import Refusal, Runner
from mirrorwall import engine, model, qualify, runtime, telemetry, video
from mirrorwall.requests import checked_request

# The code whose behaviour qualification evidence stands for. A change to it either alters what a
# qualification measures or offers (bump engine.WORKFLOW_CONTRACT, and every model re-qualifies) or
# does not (renew the pin below). Renewing it states that the change needs no new evidence.
CONTRACT_CODE = (video.defaults, video.limits, video.frame_count, video.native_payload, model.defaults, model.limits,
                 telemetry.envelope, telemetry.Meter, runtime.native_command, runtime.Native.start, runtime.Native.prepare_loras,
                 runtime.Native.prepare_video_catalog, runtime.Native.load_video_recipe, engine.SDCpp.launch, engine.SDCpp.host_ram_budget)
CONTRACT_PINS = {1: '29e8da804598e73e707b337fe9090f9189893b339204a0ce276f8b793030ed7c'}


def contract_code_digest():
    return hashlib.sha256("\n".join(inspect.getsource(one) for one in CONTRACT_CODE).encode()).hexdigest()


def test_workflow_contract_is_bumped_or_renewed_when_its_code_changes():
    assert CONTRACT_PINS.get(engine.WORKFLOW_CONTRACT) == contract_code_digest(), (
        "Code that qualification evidence stands for changed. If it alters staging, tiling, the native payload or command, "
        "a memory-relevant default or limit, the envelope or metering, bump mirrorwall.engine.WORKFLOW_CONTRACT; "
        f"otherwise renew CONTRACT_PINS[{engine.WORKFLOW_CONTRACT}] = {contract_code_digest()!r}.")


@pytest.fixture
def native(tmp_path):
    release = tmp_path / "releases" / "one" / "bin"
    release.mkdir(parents=True)
    for name in ("sd-server", "sd-cli"):
        (release / name).write_bytes(name.encode())
    engine.stamped_build.cache_clear()
    return {"executable": str(release / "sd-server"), "cli": str(release / "sd-cli")}


def test_identity_follows_native_programs_and_contract_not_python_files(native, monkeypatch):
    first = engine.build_identity(native)
    assert first.startswith("sdcpp:") and engine.build_identity(native) == first
    # Python workflow files are not in the identity: a code-only deploy keeps evidence.
    assert not any(str(path).endswith(".py") for path in engine.native_files(native))
    monkeypatch.setattr(engine, "WORKFLOW_CONTRACT", engine.WORKFLOW_CONTRACT + 1)
    stamps = tuple((str(path), 1, 1, 1) for path in engine.native_files(native))
    assert engine.stamped_build(stamps, engine.WORKFLOW_CONTRACT) != engine.stamped_build(stamps, engine.WORKFLOW_CONTRACT - 1)
    time.sleep(0.01)
    Path(native["executable"]).write_bytes(b"rebuilt server")
    assert engine.build_identity(native) != first


def image_profile(tmp_path, **sdcpp):
    weights = tmp_path / "weights.gguf"; weights.write_bytes(b"x")
    components = {name: str(tmp_path / f"{name}.gguf") for name in ("text_encoder", "vae")}
    for path in components.values():
        Path(path).write_bytes(b"x")
    return {"engine": "sdcpp", "flags": {"model": str(weights), "ctx-size": 0}, "gpus": 1, "alone": True,
            "sdcpp": {"family": "flux2-dev", "components": components, "staged": True, "host_ram_mib": 4096, **sdcpp}}


def runner_with(tmp_path, settings, build="sdcpp:new", **config):
    library = Models(":memory:"); library.put("flux", settings)
    runner = Runner(Config(gpus=("CUDA0",), **config), None, models=library)
    runner.builds[("sdcpp", engine_for(settings).installation(runner.config).executable)] = (None, build)
    return runner, library


def test_earlier_build_evidence_is_offered_flagged_until_this_build_has_its_own(tmp_path):
    settings = image_profile(tmp_path)
    runner, library = runner_with(tmp_path, settings)
    assert runner.image_evidence("flux", settings) == ({}, False)  # never qualified: nothing to re-qualify
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:old"})
    profile, stale = runner.image_evidence("flux", settings)
    assert profile["build"] == "sdcpp:old" and stale
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:new"})
    assert runner.image_evidence("flux", settings)[1] is False


def test_legacy_evidence_carries_over_only_when_the_native_programs_are_unchanged_since(tmp_path, native):
    settings = image_profile(tmp_path)
    runner, library = runner_with(tmp_path, settings, engines={"sdcpp": {"enabled": True, **native}})
    changed = os.stat(native["executable"]).st_mtime
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:python-too", "at": changed + 60})
    runner.carry_evidence(settings, "sdcpp:new")
    profile, stale = runner.image_evidence("flux", settings)
    assert not stale and profile["build"] == "sdcpp:new" and profile["carried_from"] == "sdcpp:python-too"
    # Recorded before the programs last changed, or under a contract: stays stale.
    for old in ({"qualified": True, "build": "sdcpp:older", "at": changed - 60},
                {"qualified": True, "build": "sdcpp:older", "at": changed + 60, "contract": 1}):
        library.measured("flux", settings, [9000], image_profile=old)
        runner.carry_evidence(settings, "sdcpp:new")
        assert runner.image_evidence("flux", settings) == (old, True), old
    # A later release beside the configured one could have recorded it: nothing carries over.
    newer = Path(native["executable"]).parent.parent.parent / "two" / "bin"; newer.mkdir(parents=True)
    (newer / "sd-server").write_bytes(b"later")
    os.utime(newer / "sd-server", (changed + 30, changed + 30))
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:python-too", "at": changed + 60})
    runner.carry_evidence(settings, "sdcpp:new")
    assert runner.image_evidence("flux", settings)[1]


def test_an_ordinary_run_on_a_new_build_keeps_the_earlier_qualified_envelope(tmp_path):
    settings = image_profile(tmp_path)
    runner, library = runner_with(tmp_path, settings)
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:old", "gpu_peak_mib": [8000], "rss_peak_mib": 900})

    class Health:
        async def get(self, *a, **k):
            return httpx.Response(200, json={"image_profile": {"successful": True, "complete": True, "qualified": False,
                                                                "gpu_peak_mib": [5000], "rss_peak_mib": 500}})
    runner.client = Health()
    server = SimpleNamespace(model="flux", settings=settings, cards=("CUDA0",), url="http://child", headers={}, build="sdcpp:new",
                             mib=[9000], image_profile=None, process=SimpleNamespace(pid=1, returncode=None))
    runner.sample_resident = lambda server: None
    asyncio.run(runner.measure(server))
    assert runner.image_evidence("flux", settings) == ({"qualified": True, "build": "sdcpp:old", "gpu_peak_mib": [8000], "rss_peak_mib": 900}, True)


def test_the_qualifying_image_request_is_the_largest_configured_envelope(tmp_path):
    settings = image_profile(tmp_path, limits={"max_width": 1024, "max_height": 1024, "max_references": 2})
    path, body = qualify.request("flux", settings)
    assert path == "/v1/images/generations" and len(body["references"]) == 2
    assert telemetry.envelope(checked_request(body, settings), settings)
    clip = {**image_profile(tmp_path), "sdcpp": {**image_profile(tmp_path)["sdcpp"], "mode": "vid_gen", "family": "wan2.1"}}
    path, body = qualify.request("wan", clip)
    assert path == "/v1/videos/generations" and video.checked_request(body, clip, deferred=True)["frames"] > 1


def png(colour="red", size=(640, 480)):
    output = io.BytesIO(); Image.new("RGB", size, colour).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


def test_only_a_hash_and_small_frames_are_kept_from_the_output(tmp_path):
    kept = qualify.sample({"data": [{"b64_json": png()}]}, video=False)
    assert kept["sha256"] == hashlib.sha256(base64.b64decode(png())).hexdigest() and len(kept["frames"]) == 1
    with Image.open(io.BytesIO(base64.b64decode(kept["frames"][0]))) as frame:
        assert frame.format == "JPEG" and max(frame.size) <= qualify.THUMBNAIL
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is needed to decode a WebM")
    clip = tmp_path / "clip.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=16:duration=1", "-c:v", "libvpx-vp9", str(clip)], check=True)
    kept = qualify.sample({"data": [{"b64_json": base64.b64encode(clip.read_bytes()).decode()}]}, video=True)
    assert len(kept["frames"]) == qualify.FRAMES and len(set(kept["frames"])) == qualify.FRAMES
    with pytest.raises(ValueError):
        qualify.sample({"data": [{"b64_json": ""}]}, video=False)


class Log:
    def __init__(self):
        self.added = []

    def add(self, *what, **detail):
        self.added.append((what, detail))


def qualifier(tmp_path, runner, library, answer, idle=lambda: True, **config):
    async def child(request):
        return answer(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(child))
    runner.config.auto_qualify = config.get("auto_qualify", True)
    runner.config.machine_name = "rig"
    log = Log()
    return Qualifier(runner, library, client, runner.config, log, lambda: tmp_path / "qualification", idle), log


def admitting(runner, measured=None):
    """Admit a ticket onto a fake child; `measured` is the evidence its run leaves."""
    server = SimpleNamespace(url="http://child", headers={"authorization": "Bearer private"}, model="flux")
    async def acquire(ticket):
        assert ticket.priority == "low" and ticket.user == "auto-qualify"
        ticket.server = server
        return ticket
    async def measure(server):
        if measured:
            measured()
    runner.acquire, runner.release, runner.measure = acquire, lambda ticket: None, measure
    return server


def test_an_idle_node_requalifies_each_stale_model_once_per_build(tmp_path):
    settings = image_profile(tmp_path, limits={"max_width": 1024, "max_height": 1024})
    runner, library = runner_with(tmp_path, settings)
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:old"})
    asked = []
    def answer(request):
        asked.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer private"
        return httpx.Response(200, json={"data": [{"b64_json": png()}]})
    found, log = qualifier(tmp_path, runner, library, answer)
    admitting(runner, lambda: library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:new"}))
    assert found.state("flux", settings)["pending"]
    record = asyncio.run(found.once())
    assert record["ok"] and record["build"] == "sdcpp:new" and len(record["frames"]) == 1 and asked[0]["width"] == 1024
    assert asyncio.run(found.once()) is None and len(asked) == 1  # nothing stale is left
    state = found.state("flux", settings)
    assert not state["stale"] and state["last"]["ok"] and state["last"]["sha256"] == record["sha256"]
    assert log.added[0][0][:2] == ("model", "auto-qualified")
    assert oct(found.path("flux").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("idle,on", [(False, True), (True, False)])
def test_nothing_runs_while_busy_or_switched_off(tmp_path, idle, on):
    settings = image_profile(tmp_path)
    runner, library = runner_with(tmp_path, settings)
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:old"})
    found, _ = qualifier(tmp_path, runner, library, lambda request: pytest.fail("no run expected"), idle=lambda: idle, auto_qualify=on)
    assert asyncio.run(found.once()) is None
    assert found.state("flux", settings)["pending"] is on


@pytest.mark.parametrize("failure", ["runtime", "refused", "not qualified"])
def test_a_failed_run_marks_the_build_unqualified_alerts_and_is_not_retried(tmp_path, failure):
    settings = image_profile(tmp_path)
    runner, library = runner_with(tmp_path, settings)
    library.measured("flux", settings, [9000], image_profile={"qualified": True, "build": "sdcpp:old"})
    found, log = qualifier(tmp_path, runner, library, lambda request: httpx.Response(502, json={"error": {"message": "out of memory"}})
                           if failure == "runtime" else httpx.Response(200, json={"data": [{"b64_json": png()}]}))
    admitting(runner)
    if failure == "refused":
        async def refuse(ticket):
            raise Refusal(503, "flux cannot fit")
        runner.acquire = refuse
    record = asyncio.run(found.once())
    assert not record["ok"] and record["error"]
    profile, stale = runner.image_evidence("flux", settings)
    assert not stale and profile["auto_failed"] and profile["build"] == "sdcpp:new" and "Automatic qualification failed" in profile["problem"]
    assert found.state("flux", settings)["failed"] == profile["problem"]
    [alert] = found.alerts()
    assert alert["active"] and alert["key"] == "qualification:rig:flux" and alert["explanation"] == profile["problem"]
    assert asyncio.run(found.once()) is None  # not tried again by itself on this build
    runner.builds = {key: (None, "sdcpp:newer") for key in runner.builds}
    assert found.alerts()[0]["active"] is False  # another build: the failure no longer stands
    assert log.added[0][0][1] == "auto-qualification-failed"


def test_the_post_deploy_smoke_check_makes_text_and_start_image_clips_and_removes_them(monkeypatch, tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke_video", Path(__file__).parent.parent / "scripts" / "smoke-video.py")
    smoke = importlib.util.module_from_spec(spec); spec.loader.exec_module(smoke)
    made, deleted = [], []
    def answer(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "wan", "capabilities": {"video_generation": True}, "video_workflow": {"defaults": {"width": 320, "height": 192}, "limits": {"max_seconds": 5},
                                                                                          "qualification": {"stale": True, "pending": True}}},
                {"id": "broken", "capabilities": {"video_generation": True}, "video_workflow": {"qualification": {"failed": "Automatic qualification failed: out of memory"}}},
                {"id": "chat", "capabilities": {}}]})
        if request.method == "POST" and request.url.path == "/v1/videos":
            made.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"video_{len(made)}", "status": "completed"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"\x1a\x45\xdf\xa3webm")
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={})
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(answer), **kwargs))
    key = tmp_path / "key"; key.write_text("sk-test\n")
    monkeypatch.setattr("sys.argv", ["smoke-video.py", "--url", "http://console", "--key-file", str(key)])
    with pytest.raises(SystemExit) as ended:
        smoke.main()
    out = capsys.readouterr().out
    assert ended.value.code == 1 and "FAIL broken: Automatic qualification failed" in out and "re-qualification pending" in out
    assert [one["model"] for one in made] == ["wan", "wan"] and "input_reference" not in made[0]
    assert made[1]["input_reference"].startswith("data:image/png;base64,") and made[0]["width"] == 320
    assert deleted == ["/v1/videos/video_1", "/v1/videos/video_2"] and "sk-test" not in out
