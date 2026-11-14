#!/usr/bin/env python3
"""Meter isolated igen/SD.cpp processes under openWeightGym's existing external-work lock.

Run with openWeightGym's Python; the igen child uses its existing environment. Results,
raw 100 ms telemetry, logs and images go in a private, new output directory.
This is a development comparison, not a second production scheduler or benchmark
score. Process-cold timings do not flush the OS file cache. NF4 versus GGML Q4
is a comparison of practical configurations, not identical numerical precision.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import html
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loadcoach.processes import ProcessGroup, adopt_orphans

MIB = 2**20
PROTOCOL = 1
MARKER = "IMAGE_COMPARE "
PROMPT = "A small red wooden toy sailboat on a blue cloth, studio product photograph, soft daylight, detailed wood grain."


def write_json(path, value):
    """Atomically replace a machine-readable comparison record."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def digest(path):
    """Hash a complete native binary or model artifact."""
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while piece := stream.read(8 * MIB):
            h.update(piece)
    return h.hexdigest()


def available_ram_mib():
    """Read available host RAM independently of either image allocator."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("Host memory observation is unavailable")


class NVML:
    """Driver-level device memory, independent of either runtime's allocator."""

    class Memory(ctypes.Structure):
        """Match NVML device-memory counters."""
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("total", "free", "used")]

    class Process(ctypes.Structure):
        """Match NVML per-process memory records."""
        _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong),
                    ("gpu_instance", ctypes.c_uint), ("compute_instance", ctypes.c_uint)]

    def __init__(self, indices):
        """Initialize private ownership and measurement state."""
        self.library = ctypes.CDLL("libnvidia-ml.so.1")
        self.check(self.library.nvmlInit_v2())
        self.handles = {}
        self.identity = {}
        for index in indices:
            handle = ctypes.c_void_p()
            self.check(self.library.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)))
            self.handles[index] = handle
            uuid, name = ctypes.create_string_buffer(96), ctypes.create_string_buffer(128)
            self.check(self.library.nvmlDeviceGetUUID(handle, uuid, len(uuid)))
            self.check(self.library.nvmlDeviceGetName(handle, name, len(name)))
            self.identity[str(index)] = {"uuid": uuid.value.decode(), "name": name.value.decode()}

    @staticmethod
    def check(code):
        """Fail closed when the GPU driver cannot report usage."""
        if code:
            raise RuntimeError(f"NVML observation failed ({code})")

    def snapshot(self):
        """Read device memory and compute-process ownership from the driver."""
        rows = {}
        for index, handle in self.handles.items():
            memory = self.Memory()
            self.check(self.library.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)))
            count = ctypes.c_uint(128)
            processes = (self.Process * count.value)()
            self.check(self.library.nvmlDeviceGetComputeRunningProcesses_v2(handle, ctypes.byref(count), processes))
            rows[str(index)] = {"used_mib": memory.used / MIB, "free_mib": memory.free / MIB,
                                "total_mib": memory.total / MIB,
                                "compute_pids": [processes[i].pid for i in range(count.value)]}
        return rows


class Meter:
    """Sample owned processes and device peaks throughout every workflow stage."""
    def __init__(self, nvml, group, path, baseline, minimum_ram=2048):
        """Initialize private ownership and measurement state."""
        self.nvml, self.group, self.path, self.baseline = nvml, group, path, baseline
        self.minimum_ram = minimum_ram
        self.rows, self.phase, self.problem = [], "initialization", None
        self.stop_event = asyncio.Event()

    async def run(self):
        """Collect bounded resource observations until the owning workflow stops."""
        with self.path.open("w") as stream:
            self.stream = stream
            while not self.stop_event.is_set():
                try:
                    self.sample()
                except Exception as problem:
                    self.problem = str(problem)
                    self.group.signal(signal.SIGTERM)
                    return
                try:
                    await asyncio.wait_for(self.stop_event.wait(), .1)
                except TimeoutError:
                    pass

    def sample(self):
        """Measure current owned GPU and host memory usage."""
        members = self.group.members()
        rss = 0
        for pid in members:
            try:
                rss += int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            except (OSError, IndexError):
                pass
        gpu = self.nvml.snapshot()
        row = {"monotonic": time.monotonic(), "phase": self.phase,
               "rss_tree_mib": rss / MIB, "available_ram_mib": available_ram_mib(), "gpu": gpu}
        self.rows.append(row)
        self.stream.write(json.dumps(row) + "\n")
        self.stream.flush()
        if any(pid not in members for item in gpu.values() for pid in item["compute_pids"]):
            raise RuntimeError("Foreign GPU compute appeared; measurement interrupted")
        if row["available_ram_mib"] < self.minimum_ram:
            raise RuntimeError("Host RAM safety headroom reached; measurement interrupted")
        if any(item["free_mib"] < 256 for item in gpu.values()):
            raise RuntimeError("GPU safety headroom reached; measurement interrupted")

    def summary(self, phase):
        """Summarize the requested workflow phase against its initial baseline."""
        return summarize(self.rows, phase, self.baseline)


def summarize(rows, phase, baseline):
    """Calculate observed peaks and measurement gaps without allocator estimates."""
    chosen = [row for row in rows if row["phase"] == phase]
    if not chosen:
        return {"sample_count": 0, "peak_rss_tree_mib": None, "peak_gpu_mib": None,
                "peak_gpu_total_mib": None, "largest_sample_gap_seconds": None}
    indices = list(baseline)
    delta = lambda row, index: max(0, row["gpu"][index]["used_mib"] - baseline[index]["used_mib"])
    return {"sample_count": len(chosen),
            "peak_rss_tree_mib": max(row["rss_tree_mib"] for row in chosen),
            "minimum_available_ram_mib": min(row["available_ram_mib"] for row in chosen),
            "peak_gpu_mib": {index: max(delta(row, index) for row in chosen) for index in indices},
            "peak_gpu_total_mib": max(sum(delta(row, index) for index in indices) for row in chosen),
            "largest_sample_gap_seconds": max((b["monotonic"] - a["monotonic"] for a, b in zip(chosen, chosen[1:])), default=0)}


async def benchmark_queue(host, database):
    """Read only the owning the control node's queue; never mutate/clear/pause another run."""
    code = ("import sqlite3,json; "
            f"d=sqlite3.connect('file:'+{database!r}+'?mode=ro',uri=True); "
            "print(json.dumps(d.execute(\"SELECT state,count(*) FROM benchmark_runs WHERE state IN ('queued','running') GROUP BY state\").fetchall()))")
    child = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                                 host, "python3", "-", stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await asyncio.wait_for(child.communicate(code.encode()), 15)
    if child.returncode:
        raise RuntimeError("Cannot establish the control node benchmark queue state")
    if json.loads(out):
        raise RuntimeError("Benchmarks are queued/running; existing work must drain first")


def idle(status):
    """Decide whether the observed openWeightGym status has drained active work."""
    required = {"running", "waiting", "others_running", "others_waiting", "active_batches", "batches", "busy", "lock", "draining"}
    if not required <= status.keys():
        raise RuntimeError("openWeightGym work state is incomplete; resources were not taken")
    if any(status.get(key) for key in ("running", "waiting", "others_running", "others_waiting", "active_batches", "batches", "busy", "lock", "draining")):
        raise RuntimeError("openWeightGym has active/queued/locked work; comparison did not take resources")


class Lease:
    """Maintain the existing openWeightGym external-work lock during isolated qualification."""
    def __init__(self, args):
        """Initialize private ownership and measurement state."""
        import httpx
        key = args.suite_key_file.read_text().strip()
        self.client = httpx.AsyncClient(base_url=args.suite_url, timeout=20,
                                        headers={"authorization": "Bearer " + key, "x-turnstile-no-wake": "1"})
        self.token, self.problem = None, None

    async def ask(self, method, path, body=None):
        """Call the existing control node API with a private server-side credential."""
        headers = {"X-LoadCoach-Lock": self.token} if self.token else {}
        response = await self.client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"openWeightGym {path} returned HTTP {response.status_code}")
        return response.json()

    async def acquire(self):
        """Acquire exclusive external-work admission after the queue drains."""
        status = await self.ask("GET", "/loadcoach/status")
        if status.get("hostname") != socket.gethostname():
            raise RuntimeError("openWeightGym's selected compute host is not this benchmark host")
        idle(status)
        self.token = (await self.ask("POST", "/loadcoach/lock", {"seconds": 90, "unload": True}))["token"]

    async def renew(self):
        """Renew admission while the isolated native processes remain alive."""
        while True:
            await asyncio.sleep(10)
            try:
                await self.ask("POST", "/loadcoach/lock", {"seconds": 90})
                status = await self.ask("GET", "/loadcoach/status")
                if status.get("waiting") or status.get("others_waiting"):
                    raise RuntimeError("Foreground requests are waiting; comparison yields resources")
            except Exception as problem:
                self.problem = str(problem)
                return

    async def close(self):
        """Release the external-work lease after process cleanup."""
        try:
            if self.token:
                await self.ask("POST", "/loadcoach/unlock")
        finally:
            await self.client.aclose()


def worker_emit(value):
    """Send one structured child result to the supervising comparison process."""
    print(MARKER + json.dumps(value), flush=True)


def worker(job_path):
    """Existing igen graph/memory manager; no replacement inference implementation."""
    job = json.loads(job_path.read_text())
    sys.path.insert(0, job["igen_root"])
    if job.get("inspect"):
        from igen.registry import entry, upscaler
        selected = entry(job["model"])
        selected["preparation_upscaler"] = upscaler("4xNomos8kSC")
        worker_emit(selected)
        return
    import torch
    import igen.catalog  # noqa: F401 - registers graph nodes in the selected igen checkout
    from igen.backends.memory import Manager
    from igen.executor import Executor, Session
    from igen.graph import Graph
    from igen.nodes import weight_labels
    from igen.planner import plan
    from igen.registry import flux2_graph
    torch.set_num_threads(job["threads"])
    memory = Manager()
    session = Session(memory=memory)
    from importlib.metadata import version
    worker_emit({"ready": True, "torch": torch.__version__, "cuda": torch.version.cuda,
                 "packages": {name: version(name) for name in ("diffusers", "transformers", "bitsandbytes", "spandrel")}})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            session.cache.clear()  # fresh conditioning; resident weights remain managed
            if request.get("prepare"):
                from PIL import Image
                from igen.catalog.images import fit_megapixels
                source, output = request["source"], request["output"]
                with Image.open(source) as image:
                    target = fit_megapixels(*image.size, 1.0)
                    small = image.width * image.height < target[0] * target[1]
                graph = Graph()
                image = graph.add("LoadImage", path=source)
                if small:
                    up = graph.add("UpscaleModelLoader", path=job["model_settings"]["preparation_upscaler"])
                    image = graph.add("ImageUpscaleWithModel", upscale_model=up, image=image, scale=0.0, tile=512, overlap=96)
                image = graph.add("ScaleImage", image=image, width=target[0], height=target[1])
                graph.add("SaveImage", image=image, path_template=output)
            else:
                selected = job["model_settings"]
                graph = flux2_graph(model_path=selected["path"], components=selected["components"],
                                    loader=selected["loader"], quant=selected["quant"],
                                    prompt=request["prompt"], out=request["output"], seed=request["seed"],
                                    refs=request["refs"], ref_megapixels=None,
                                    width=job["width"], height=job["height"], steps=job["steps"], guidance=1.0)
            memory.begin_job(weight_labels(graph))
            for index in range(torch.cuda.device_count()):
                torch.cuda.reset_peak_memory_stats(index)
            started = time.perf_counter()
            try:
                Executor(graph, plan(graph.validate(), [len(graph.nodes) - 1]), session).run()
                for index in range(torch.cuda.device_count()):
                    torch.cuda.synchronize(index)
                stats = memory.end_job()
                worker_emit({"seconds": time.perf_counter() - started, "load_seconds": stats["load_seconds"],
                             "node_stats": memory.nodes, "memory_stats": stats,
                             "igen_primary_allocator_reserved_peak_mib": stats["peak_vram"] / MIB})
            except Exception as problem:
                worker_emit({"error": f"{type(problem).__name__}: {problem}"})
                return
    finally:
        memory.close()


async def read_worker(child, log):
    """Read child measurement records while retaining their private log."""
    while line := await child.stdout.readline():
        text = line.decode(errors="replace")
        log.write(text)
        log.flush()
        if text.startswith(MARKER):
            return json.loads(text[len(MARKER):])
    raise RuntimeError("igen child exited before returning a measurement")


async def guarded(awaitable, meter, lease, timeout):
    """Abort a measurement when admission, resource headroom or its timeout fails."""
    task = asyncio.ensure_future(awaitable)
    began = time.monotonic()
    try:
        while not task.done():
            problem = meter.problem or lease.problem
            if problem:
                raise RuntimeError(problem)
            if time.monotonic() - began > timeout:
                raise TimeoutError("Runtime measurement exceeded its timeout")
            await asyncio.wait({task}, timeout=.1)
        value = task.result()
        if meter.problem or lease.problem:
            raise RuntimeError(meter.problem or lease.problem)
        return value
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def free_port():
    """Obtain an unused private loopback listener port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def native_command(args, selected, cards, port):
    """Build native arguments from the fixed comparison profile."""
    components = selected["components"]
    transformer = args.native_transformer or Path(components["unet"]) / "diffusion_pytorch_model.safetensors.index.json"
    encoder = args.native_encoder or Path(components["clip"]) / "model.safetensors.index.json"
    vae = Path(components["vae"]) / "diffusion_pytorch_model.safetensors"
    for path in (transformer, encoder, vae):
        if not path.is_file():
            raise ValueError(f"Required native component source is missing: {path}")
    return [str(args.native_server), "--listen-ip", "127.0.0.1", "--listen-port", str(port),
            "--diffusion-model", str(transformer), "--llm", str(encoder), "--vae", str(vae),
            "--type", args.native_type, "--tensor-type-rules", r"^(vae|first_stage_model)\.=bf16",
            "--backend", "diffusion=cuda0,vae=cuda0,te=" + ("cuda1" if cards == 2 else "cuda0"),
            "--max-vram", "-1", "--conditioning-cache-size", "0", "--rng", "cpu",
            "--threads", str(args.threads), "--flash-attn", "on"]


async def native_ready(client, child):
    """Wait for the native model catalogue or a bounded startup failure."""
    import httpx
    while True:
        if child.returncode is not None:
            raise RuntimeError(f"Native runtime exited ({child.returncode}); see its log")
        try:
            response = await client.get("/v1/models")
            if response.status_code == 200 and response.json().get("data"):
                return response.json()
        except httpx.HTTPError:
            pass
        await asyncio.sleep(.2)


async def native_image(client, request, args):
    """Generate one synthetic qualification image from fixed settings."""
    payload = {"prompt": request["prompt"], "seed": request["seed"], "width": args.width, "height": args.height,
               "batch_count": 1, "sample_params": {"sample_steps": args.steps, "sample_method": "euler",
                   "scheduler": "flux2", "guidance": {"txt_cfg": 1.0}}, "output_format": "png",
               "ref_images": [base64.b64encode(Path(path).read_bytes()).decode() for path in request["refs"]]}
    if request["refs"]:
        payload.update(image_preprocess="target=ref,mode=none", ref_image_args="resize_before_vae=false")
    response = await client.post("/sdcpp/v1/img_gen", json=payload)
    response.raise_for_status()
    number = response.json()["id"]
    while True:
        response = await client.get("/sdcpp/v1/jobs/" + number)
        response.raise_for_status()
        result = response.json()
        if result["status"] == "completed":
            image = result["result"]["images"][0]
            Path(request["output"]).write_bytes(base64.b64decode(image["b64_json"], validate=True))
            return {"native_job_seconds": (result["completed"] - result["started"]) / 1000,
                    "native_job_id": number, "submitted_parameters": {k: v for k, v in payload.items() if k != "ref_images"}}
        if result["status"] in ("failed", "cancelled"):
            raise RuntimeError("Native job " + result["status"] + ": " + json.dumps(result.get("error")))
        await asyncio.sleep(.05)


def validate_image(path, dimensions):
    """Verify output dimensions and return its content identity."""
    from PIL import Image, ImageStat
    with Image.open(path) as image:
        image.load()
        if image.size != dimensions:
            raise RuntimeError(f"Output dimensions {image.size} do not match {dimensions}")
        if max(ImageStat.Stat(image.convert("RGB")).stddev) < .5:
            raise RuntimeError("Output is effectively blank")
    return digest(path)


async def run_profile(args, selected, nvml, runtime, cards, refs, record):
    """Run one immutable runtime profile with owned cleanup and driver sampling."""
    import httpx
    label = f"{runtime}-{cards}gpu"
    folder = args.output / label
    folder.mkdir()
    await benchmark_queue(args.benchmark_host, args.benchmark_database)
    lease = Lease(args)
    child = group = meter = meter_task = renewal = client = None
    succeeded = False
    log = (folder / "runtime.log").open("w")
    try:
        await lease.acquire()
        await asyncio.sleep(.3)  # allow the previous CUDA context's cleanup to settle
        baseline = nvml.snapshot()
        write_json(folder / "baseline.json", baseline)
        if any(row["compute_pids"] for row in baseline.values()):
            raise RuntimeError("Foreign GPU compute is present after openWeightGym admission")
        if available_ram_mib() < 4096:
            raise RuntimeError("Insufficient host RAM headroom before starting")
        renewal = asyncio.create_task(lease.renew())
        environment = dict(os.environ, CUDA_VISIBLE_DEVICES=",".join(nvml.identity[str(i)]["uuid"] for i in args.gpus[:cards]),
                           OMP_NUM_THREADS=str(args.threads), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           TOKENIZERS_PARALLELISM="false")
        port = free_port()
        job = {"igen_root": str(args.igen_root), "model_settings": selected,
               "width": args.width, "height": args.height, "steps": args.steps, "threads": args.threads}
        write_json(folder / "worker.json", job)
        command = ([str(args.igen_python), str(Path(__file__).resolve()), "--worker", str(folder / "worker.json")]
                   if runtime in ("igen", "prepare") else native_command(args, selected, cards, port))
        write_json(folder / "launch.json", {"command": command, "visible_gpus": environment["CUDA_VISIBLE_DEVICES"]})
        started = time.monotonic()
        child = await asyncio.create_subprocess_exec(*command, cwd=args.igen_root, env=environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE if runtime != "sdcpp" else log,
                    stderr=log, start_new_session=True, limit=2**22)
        group = ProcessGroup(child)
        meter = Meter(nvml, group, folder / "telemetry.jsonl", baseline)
        meter_task = asyncio.create_task(meter.run())
        if runtime != "sdcpp":
            ready = await guarded(read_worker(child, log), meter, lease, args.timeout)
        else:
            client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30)
            ready = await guarded(native_ready(client, child), meter, lease, args.timeout)
        if ready.get("error"):
            raise RuntimeError(ready["error"])
        init_seconds = time.monotonic() - started
        meter.sample()
        record({"kind": "initialization", "runtime": runtime, "cards": cards,
                "seconds": init_seconds, "details": ready, **meter.summary("initialization")})
        first_image = True
        cases = args.reference_counts if runtime != "prepare" else [0]
        for reference_count in cases:
            for repeat in range(args.repeats + 1 if runtime != "prepare" else 2):
                name = f"refs{reference_count}-sample{repeat}"
                output = folder / (name + ".png")
                request = {"prompt": PROMPT if not reference_count else
                           "Photograph the toy boat from image 1 on a wooden table beside a window, preserve its colors and design."
                           + (" Include the patterned fabric from image 2 as the tablecloth." if reference_count == 2 else ""),
                           "seed": 100 + repeat, "refs": [str(path) for path in refs[:reference_count]], "output": str(output)}
                if runtime == "prepare":
                    request = {"prepare": True, "source": str(refs[repeat]), "output": str(args.output / "fixtures" / f"reference-{repeat + 1}.png")}
                meter.phase = name
                meter.sample()
                began = time.monotonic()
                if runtime != "sdcpp":
                    child.stdin.write((json.dumps(request) + "\n").encode())
                    await child.stdin.drain()
                    result = await guarded(read_worker(child, log), meter, lease, args.timeout)
                else:
                    result = await guarded(native_image(client, request, args), meter, lease, args.timeout)
                elapsed = time.monotonic() - began
                finished = time.monotonic()
                meter.sample()
                if result.get("error"):
                    raise RuntimeError(result["error"])
                from PIL import Image
                artifact = Path(request["output"])
                with Image.open(artifact) as image:
                    dimensions = (args.width, args.height) if runtime != "prepare" else image.size
                checksum = validate_image(artifact, dimensions)
                row = {"kind": "image" if runtime != "prepare" else "reference_preparation", "runtime": runtime,
                       "cards": cards, "references": reference_count, "repeat": repeat,
                       "warmup": repeat == 0, "seconds": elapsed,
                       "cold_process_to_first_image_seconds": finished - started if first_image else None,
                       "output": str(artifact.relative_to(args.output)), "sha256": checksum,
                       "details": result, **meter.summary(name)}
                record(row)
                first_image = False
                print(f"{label} {name}: {elapsed:.2f}s, GPU {row['peak_gpu_total_mib']:.0f} MiB, RAM {row['peak_rss_tree_mib']:.0f} MiB", flush=True)
        succeeded = True
    except BaseException as problem:
        interrupted = isinstance(problem, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
        record({"kind": "profile_failure", "runtime": runtime, "cards": cards,
                "error": f"{type(problem).__name__}: {problem}", "interrupted": interrupted})
        print(f"{label} failed: {type(problem).__name__}: {problem}", flush=True)
        if interrupted:
            raise
    finally:
        if child and child.stdin:
            child.stdin.close()
        # Resource ownership outlives the child wrapper until its whole session exits.
        if group:
            await group.stop(10)
        if meter:
            meter.stop_event.set()
        if meter_task:
            await meter_task
        if client:
            await client.aclose()
        if group:
            for _ in range(100):
                if not any(row["compute_pids"] for row in nvml.snapshot().values()):
                    break
                await asyncio.sleep(.1)
            else:
                raise RuntimeError("GPU cleanup not confirmed; no subsequent profile started")
        if renewal:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        await lease.close()
        log.close()
    if succeeded:
        record({"kind": "profile_complete", "runtime": runtime, "cards": cards})


def report(root, rows):
    """Render the comparison results and output links from retained measurements."""
    summaries = []
    for runtime, cards, refs in sorted({(r["runtime"], r["cards"], r["references"]) for r in rows if r["kind"] == "image"}):
        samples = [r for r in rows if r["kind"] == "image" and (r["runtime"], r["cards"], r["references"]) == (runtime, cards, refs) and not r["warmup"]]
        if not samples:
            continue
        summaries.append({"runtime": runtime, "cards": cards, "references": refs, "measured_images": len(samples),
                          "median_seconds": statistics.median(r["seconds"] for r in samples),
                          "min_seconds": min(r["seconds"] for r in samples), "max_seconds": max(r["seconds"] for r in samples),
                          "peak_gpu_mib": {index: max(r["peak_gpu_mib"][index] for r in samples) for index in samples[0]["peak_gpu_mib"]},
                          "peak_gpu_total_mib": max(r["peak_gpu_total_mib"] for r in samples),
                          "peak_rss_tree_mib": max(r["peak_rss_tree_mib"] for r in samples)})
    profiles = []
    for runtime, cards in sorted({(r["runtime"], r["cards"]) for r in rows if r["kind"] == "image"}):
        observed = [r for r in rows if r.get("runtime") == runtime and r.get("cards") == cards and r["kind"] in ("image", "initialization")]
        first = next(r for r in observed if r["kind"] == "image")
        warm = [r for r in observed if r["kind"] == "image" and not r["warmup"]]
        profiles.append({"runtime": runtime, "cards": cards,
            "cold_process_to_first_image_seconds": first["cold_process_to_first_image_seconds"],
            "lifecycle_peak_gpu_total_mib": max(r["peak_gpu_total_mib"] for r in observed),
            "lifecycle_peak_rss_tree_mib": max(r["peak_rss_tree_mib"] for r in observed),
            "warm_peak_rss_tree_mib": max((r["peak_rss_tree_mib"] for r in warm), default=None)})
    failures = [row for row in rows if row["kind"] in ("profile_failure", "run_failure")]
    manifest = json.loads((root / "manifest.json").read_text())
    expected = {(runtime, cards) for runtime in manifest["runtimes"] for cards in manifest["layouts"]}
    completed = {(r["runtime"], r["cards"]) for r in rows if r["kind"] == "profile_complete"}
    complete_samples = all(any(s["runtime"] == runtime and s["cards"] == cards and s["references"] == ref and
                              s["measured_images"] == manifest["repeats"] for s in summaries)
                           for runtime, cards in expected for ref in manifest["reference_counts"])
    status = ("interrupted" if any(r.get("interrupted") for r in failures) else "failed") if failures else (
        "complete" if expected <= completed and complete_samples else "in_progress")
    write_json(root / "summary.json", {"protocol": PROTOCOL, "report_version": 2, "status": status,
        "profiles": profiles, "measurements": summaries, "failures": failures})
    table = "".join(f"<tr><td>{s['runtime']}</td><td>{s['cards']}</td><td>{s['references']}</td><td>{s['measured_images']}</td><td>{s['median_seconds']:.2f}</td><td>{' / '.join(f'{v/1024:.2f}' for v in s['peak_gpu_mib'].values())}</td><td>{s['peak_gpu_total_mib']/1024:.2f}</td><td>{s['peak_rss_tree_mib']/1024:.2f}</td></tr>" for s in summaries)
    starts = "".join(f"<tr><td>{s['runtime']}</td><td>{s['cards']}</td><td>{s['cold_process_to_first_image_seconds']:.2f}</td><td>{s['lifecycle_peak_gpu_total_mib']/1024:.2f}</td><td>{s['lifecycle_peak_rss_tree_mib']/1024:.2f}</td><td>{s['warm_peak_rss_tree_mib']/1024:.2f}</td></tr>" for s in profiles if s["warm_peak_rss_tree_mib"] is not None)
    images = "".join(f"<figure><img loading='lazy' src='{html.escape(r['output'], quote=True)}'><figcaption>{r['runtime']} · {r['cards']} GPU · {r['references']} refs · seed {100+r['repeat']}</figcaption></figure>" for r in rows if r["kind"] == "image" and r["repeat"] == 1)
    errors = "".join("<li>" + html.escape(json.dumps(item)) + "</li>" for item in failures)
    (root / "report.html").write_text("<!doctype html><meta charset='utf-8'><title>Image runtime comparison</title><style>body{font:16px system-ui;margin:2rem;background:#161b22;color:#ddd}td,th{padding:.6rem;text-align:left;border-bottom:1px solid #555}section{display:flex;flex-wrap:wrap}figure{margin:1rem;width:300px}img{width:100%}</style><h1>Image runtime comparison</h1><p>Status: " + status + ". Measurement protocol 1; report format 2.</p><p>Practical NF4 versus prepared GGML Q4_0 configurations. Same source weights, inputs and dimensions; quantization/RNG/numerical paths differ. Weight preparation is separate from inference timing. Process-cold does not mean disk-cold. GPU: sampled driver usage above clean baseline. Per-card maxima are independent; total is the maximum simultaneous usage. RAM is sampled process-tree RSS, not a qualified service/cgroup admission budget.</p><h2>Loading and lifecycle</h2><p>First-image latency is one process-cold observation per profile, including imports, lazy loading, initial generation and writing. Lifecycle peaks cover all stages. Warm RAM excludes initialization and per-case warm-ups; component reloads within measured requests remain included.</p><table><tr><th>Runtime</th><th>GPUs</th><th>First image seconds</th><th>Lifecycle GPU GiB</th><th>Lifecycle RAM GiB</th><th>Warm RAM GiB</th></tr>" + starts + "</table><h2>Warm image requests</h2><p>" + str(manifest['repeats']) + " measured images after one warm-up per reference count. Timings include fresh prompt/reference processing, execution and output writing; no cross-request conditioning cache.</p><table><tr><th>Runtime</th><th>GPUs</th><th>References</th><th>Images</th><th>Median seconds</th><th>GPU 0 / 1 GiB</th><th>Total GPU GiB</th><th>RAM GiB</th></tr>" + table + "</table><ul>" + errors + "</ul><h2>Visual review — synthetic reference fixtures</h2><p>These test resource behavior, not human identity preservation. LoRAs and tensor splitting were not tested.</p><section>" + images + "</section>")


async def compare(args):
    """Execute the selected reproducible comparison cases under openWeightGym admission."""
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(Path(__file__).resolve(), args.output / "harness.py")
    shutil.copyfile(Path(__file__).with_name("export-igen-flux2.py"), args.output / "export-layout.py")
    fixtures = args.output / "fixtures"
    fixtures.mkdir()
    inspect_path = args.output / "inspect.json"
    write_json(inspect_path, {"inspect": True, "igen_root": str(args.igen_root), "model": args.model})
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1")
    child = await asyncio.create_subprocess_exec(str(args.igen_python), str(Path(__file__).resolve()), "--worker", str(inspect_path),
                 env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await child.communicate()
    if child.returncode:
        raise RuntimeError("Cannot inspect igen model: " + err.decode(errors="replace")[-2000:])
    selected = json.loads(next(line[len(MARKER):] for line in out.decode().splitlines() if line.startswith(MARKER)))
    if selected["family"] != "flux2":
        raise ValueError("This bounded protocol currently compares Flux 2 klein only")
    from PIL import Image, ImageDraw
    for i, size in enumerate(((640, 480), (1200, 1600)), 1):
        image = Image.new("RGB", size, "#d9e4ef")
        draw = ImageDraw.Draw(image)
        w, h = size
        if i == 1:
            draw.polygon([(w*.2,h*.7),(w*.8,h*.7),(w*.65,h*.85),(w*.35,h*.85)], fill="#bb2222")
            draw.line([(w*.5,h*.2),(w*.5,h*.7)], fill="#222222", width=6)
            draw.polygon([(w*.52,h*.2),(w*.52,h*.65),(w*.8,h*.65)], fill="white")
        else:
            for x in range(0,w,100):
                draw.rectangle((x,0,x+40,h),fill="#164e70")
            for y in range(0,h,100):
                draw.rectangle((0,y,w,y+30),fill="#bf8e52")
        image.save(fixtures / f"original-{i}.png")
    # Freeze input identities before timing; this warms filesystem cache equally.
    identities = []
    for root in sorted(set(selected["components"].values()) | {selected["preparation_upscaler"]}):
        path = Path(root)
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file in files:
            if file.suffix in (".safetensors", ".json", ".pth", ".jinja", ".txt"):
                identities.append({"path": str(file), "bytes": file.stat().st_size, "sha256": digest(file)})
    native_inputs = []
    for folder in sorted({path.parent for path in (args.native_transformer, args.native_encoder) if path}):
        for file in sorted(folder.glob("*")):
            if file.is_file() and file.suffix in (".json", ".safetensors", ".gguf"):
                native_inputs.append({"path": str(file), "bytes": file.stat().st_size, "sha256": digest(file)})
    version = await asyncio.create_subprocess_exec("git", "-C", str(args.native_source), "rev-parse", "HEAD", stdout=asyncio.subprocess.PIPE)
    commit, _ = await version.communicate()
    nvml = NVML(args.gpus)
    write_json(args.output / "manifest.json", {"protocol": PROTOCOL, "source_model": selected, "source_files": identities,
               "native_input_files": native_inputs, "script_sha256": digest(Path(__file__).resolve()), "native_commit": commit.decode().strip(),
               "native_binary_sha256": digest(args.native_server), "python_quantization": selected["quant"],
               "native_quantization": args.native_type, "width": args.width, "height": args.height, "steps": args.steps,
               "repeats": args.repeats, "layouts": args.layouts, "runtimes": args.runtimes, "reference_counts": args.reference_counts,
               "hostname": socket.gethostname(), "gpus": nvml.identity, "system": list(os.uname()),
               "igen_source_sha256": {str(path.relative_to(args.igen_root)): digest(path) for path in sorted((args.igen_root / "igen").rglob("*.py"))},
               "initial_gpu": nvml.snapshot(), "measurement_interval_seconds": .1,
               "limits": ["Quantization and RNG paths differ", "OS disk caches are not flushed", "Synthetic reference fixtures; no identity quality qualification", "No production image integration or general tensor-split qualification"]})
    rows = []
    def record(row):
        """Retain one completed comparison case before advancing to the next."""
        rows.append(row)
        with (args.output / "results.jsonl").open("a") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        report(args.output, rows)
    adopt_orphans()
    try:
        originals = [fixtures / f"original-{i}.png" for i in (1,2)]
        await run_profile(args, selected, nvml, "prepare", 1, originals, record)
        refs = [fixtures / f"reference-{i}.png" for i in (1,2)]
        if not all(path.exists() for path in refs):
            raise RuntimeError("Reference preparation failed; no plain-resize fallback used")
        for index, cards in enumerate(args.layouts):
            for runtime in (args.runtimes if index % 2 == 0 else list(reversed(args.runtimes))):
                await run_profile(args, selected, nvml, runtime, cards, refs, record)
    except BaseException as problem:
        if not rows or rows[-1]["kind"] != "profile_failure":
            record({"kind": "run_failure", "error": f"{type(problem).__name__}: {problem}",
                    "interrupted": isinstance(problem, (asyncio.CancelledError, KeyboardInterrupt))})
        raise
    finally:
        report(args.output, rows)
    return 1 if any(row["kind"] == "profile_failure" for row in rows) else 0


def main():
    """Parse explicit command options and run the requested operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--igen-root", type=Path)
    parser.add_argument("--igen-python", type=Path)
    parser.add_argument("--native-source", type=Path)
    parser.add_argument("--native-server", type=Path)
    parser.add_argument("--native-transformer", type=Path, help="Verified native-layout transformer file/index when Diffusers names are unsupported")
    parser.add_argument("--native-encoder", type=Path, help="Prepared native text encoder (e.g. Q4 GGUF)")
    parser.add_argument("--runtimes", nargs="+", choices=("igen", "sdcpp"), default=["igen", "sdcpp"])
    parser.add_argument("--suite-url")
    parser.add_argument("--suite-key-file", type=Path)
    parser.add_argument("--benchmark-host")
    parser.add_argument("--benchmark-database")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default="flux2-klein")
    parser.add_argument("--native-type", default="q4_0", choices=("q4_0", "q8_0", "f16"))
    parser.add_argument("--gpus", nargs="+", type=int, default=[0,1])
    parser.add_argument("--layouts", nargs="+", type=int, choices=(1,2), default=[1,2])
    parser.add_argument("--reference-counts", nargs="+", type=int, choices=(0,1,2), default=[0,1,2])
    parser.add_argument("--width", type=int, default=832)
    parser.add_argument("--height", type=int, default=1216)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    for name in ("igen_root", "igen_python", "native_source", "native_server", "suite_url", "suite_key_file", "benchmark_host", "benchmark_database", "output"):
        if getattr(args, name) is None:
            parser.error("Missing --" + name.replace("_", "-"))
    for name in ("igen_root", "igen_python", "native_source", "native_server", "native_transformer", "native_encoder", "suite_key_file", "output"):
        if value := getattr(args, name):
            setattr(args, name, value.expanduser().resolve())
    if len(args.gpus) < max(args.layouts) or len(args.gpus) != len(set(args.gpus)):
        parser.error("Supply distinct GPUs for the requested layouts")
    if any(len(values) != len(set(values)) for values in (args.layouts, args.reference_counts, args.runtimes)):
        parser.error("Do not repeat layouts, reference counts or runtimes")
    if not 1 <= args.threads <= 32 or args.timeout < 10 or max(args.width,args.height) > 4096:
        parser.error("Use 1–32 threads, a timeout of at least 10 seconds and dimensions no greater than 4096")
    if not 1 <= args.repeats <= 10 or not 1 <= args.steps <= 50 or args.width % 16 or args.height % 16 or min(args.width,args.height) < 256:
        parser.error("Use 1–10 repeats, 1–50 steps and positive generation dimensions divisible by 16")
    raise SystemExit(asyncio.run(compare(args)))


if __name__ == "__main__":
    main()
