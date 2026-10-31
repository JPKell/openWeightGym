"""Resource/comparison contracts without loading models or touching live services."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest


spec = importlib.util.spec_from_file_location("image_comparison", Path(__file__).parents[1] / "scripts/compare-image-runtimes.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_total_gpu_peak_uses_simultaneous_observations_and_unknown_is_not_zero():
    baseline = {"0": {"used_mib": 10}, "1": {"used_mib": 20}}
    def row(t, first, second):
        return {"phase": "render", "monotonic": t, "rss_tree_mib": 200,
                "available_ram_mib": 6000, "gpu": {"0": {"used_mib": first}, "1": {"used_mib": second}}}
    result = comparison.summarize([row(0, 1010, 20), row(.1, 10, 2020)], "render", baseline)
    assert result["peak_gpu_mib"] == {"0": 1000, "1": 2000}
    assert result["peak_gpu_total_mib"] == 2000  # not the misleading sum of independent peaks
    assert comparison.summarize([], "render", baseline)["peak_gpu_total_mib"] is None


@pytest.mark.parametrize("field,value", [("waiting", [{}]), ("others_running", 1),
                                         ("active_batches", [{}]), ("lock", {"held": True})])
def test_existing_work_is_refused_before_lock_acquisition(field, value):
    status = dict(running=[], waiting=[], others_running=0, others_waiting=0,
                  active_batches=[], batches=0, busy=0, lock=None, draining=None)
    comparison.idle(status)
    status[field] = value
    with pytest.raises(RuntimeError, match="did not take resources"):
        comparison.idle(status)


def test_incomplete_work_state_is_not_treated_as_idle():
    with pytest.raises(RuntimeError, match="incomplete"):
        comparison.idle({"running": [], "waiting": []})


def test_foreign_gpu_or_low_ram_interrupts_observation(tmp_path, monkeypatch):
    class Group:
        def members(self):
            return {os_pid: "S"}
    class Driver:
        def snapshot(self):
            return {"0": {"used_mib": 100, "free_mib": 10000, "compute_pids": [os_pid, 999999]}}
    import os
    os_pid = os.getpid()
    meter = comparison.Meter(Driver(), Group(), tmp_path / "raw.jsonl", {"0": {"used_mib": 0}})
    with meter.path.open("w") as stream:
        meter.stream = stream
        with pytest.raises(RuntimeError, match="Foreign GPU"):
            meter.sample()
    monkeypatch.setattr(Driver, "snapshot", lambda _: {"0": {"used_mib": 100, "free_mib": 10000, "compute_pids": [os_pid]}})
    monkeypatch.setattr(comparison, "available_ram_mib", lambda: 100)
    with meter.path.open("w") as stream:
        meter.stream = stream
        with pytest.raises(RuntimeError, match="Host RAM"):
            meter.sample()


def test_owned_child_is_stopped_before_resources_can_be_released():
    async def run():
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
        group = comparison.ProcessGroup(child)
        await group.stop(.2)
        assert child.returncode is not None
        assert not group.members()
    asyncio.run(run())


def report_fixture(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "runtimes": ["igen"], "layouts": [1], "reference_counts": [0], "repeats": 3}))
    base = dict(runtime="igen", cards=1, peak_gpu_mib={"0": 400}, peak_gpu_total_mib=400,
                peak_rss_tree_mib=100, cold_process_to_first_image_seconds=None)
    rows = [{**base, "kind": "initialization", "peak_rss_tree_mib": 1200}]
    for repeat in range(4):
        rows.append({**base, "kind": "image", "references": 0, "repeat": repeat,
                     "warmup": repeat == 0, "seconds": 10 + repeat,
                     "cold_process_to_first_image_seconds": 30 if repeat == 0 else None,
                     "peak_rss_tree_mib": 900 if repeat == 0 else 100,
                     "output": f"image{repeat}.png"})
    return rows


def test_report_distinguishes_loading_peaks_and_requires_all_measured_samples(tmp_path):
    rows = report_fixture(tmp_path)
    completion = {"kind": "profile_complete", "runtime": "igen", "cards": 1}
    comparison.report(tmp_path, rows[:-1] + [completion])
    assert json.loads((tmp_path / "summary.json").read_text())["status"] == "in_progress"
    comparison.report(tmp_path, rows + [completion])
    result = json.loads((tmp_path / "summary.json").read_text())
    assert result["status"] == "complete"
    assert result["profiles"][0]["lifecycle_peak_rss_tree_mib"] == 1200
    assert result["profiles"][0]["warm_peak_rss_tree_mib"] == 100
    assert result["profiles"][0]["cold_process_to_first_image_seconds"] == 30
    assert result["measurements"][0]["median_seconds"] == 12
    assert result["measurements"][0]["measured_images"] == 3


@pytest.mark.parametrize("interrupted,expected", [(False, "failed"), (True, "interrupted")])
def test_report_preserves_failed_and_interrupted_run_status(tmp_path, interrupted, expected):
    rows = report_fixture(tmp_path)
    rows.append({"kind": "run_failure", "error": "stopped", "interrupted": interrupted})
    comparison.report(tmp_path, rows)
    result = json.loads((tmp_path / "summary.json").read_text())
    assert result["status"] == expected
    assert result["failures"][0]["error"] == "stopped"


def test_native_failed_job_is_not_a_timing_sample(tmp_path):
    async def run():
        def reply(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                assert payload["sample_params"]["sample_steps"] == 6
                assert payload["sample_params"]["scheduler"] == "flux2"
                assert "image_preprocess" not in payload  # no reference target on text-only jobs
                return httpx.Response(202, json={"id": "fixture"})
            return httpx.Response(200, json={"status": "failed", "error": {"message": "unsupported weights"}})
        class Args:
            width, height, steps = 832, 1216, 6
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url="http://fixture") as client:
            with pytest.raises(RuntimeError, match="unsupported weights"):
                await comparison.native_image(client, {"prompt": "fixture", "seed": 1, "refs": [],
                    "output": str(tmp_path / "absent.png")}, Args())
        assert not (tmp_path / "absent.png").exists()
    asyncio.run(run())


def test_prepared_references_keep_their_geometry_and_return_the_real_image(tmp_path):
    from PIL import Image
    import base64
    source = tmp_path / "reference.png"
    Image.new("RGB", (16, 24), "red").save(source)
    encoded = base64.b64encode(source.read_bytes()).decode()
    async def run():
        def reply(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                assert payload["ref_images"] == [encoded]
                assert payload["image_preprocess"] == "target=ref,mode=none"
                assert payload["ref_image_args"] == "resize_before_vae=false"
                return httpx.Response(202, json={"id": "fixture"})
            return httpx.Response(200, json={"status": "completed", "started": 1000, "completed": 2500,
                "result": {"images": [{"b64_json": encoded}]}})
        class Args:
            width, height, steps = 832, 1216, 6
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url="http://fixture") as client:
            result = await comparison.native_image(client, {"prompt": "fixture", "seed": 1,
                "refs": [str(source)], "output": str(tmp_path / "result.png")}, Args())
        assert result["native_job_seconds"] == 1.5
        assert (tmp_path / "result.png").read_bytes() == source.read_bytes()
    asyncio.run(run())
