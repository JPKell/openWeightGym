"""CPU-only Strata bridge contracts; real upstream checks require an explicit source path."""
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from engines import engine_for
from engines.strata import runtime


def test_strata_is_disabled_until_explicitly_configured():
    assert engine_for("strata").installation(SimpleNamespace(engines={})).enabled is False


def test_qwen36_host_estimate_counts_all_experts_and_refuses_unsafe_load():
    experts = 19671285760
    required = runtime.require_ram(experts, 22528, 24000)
    assert required == 20808
    with pytest.raises(ValueError, match="Model not loaded"):
        runtime.require_ram(experts, 22528, 14134)
    with pytest.raises(ValueError, match="Model not loaded"):
        runtime.require_ram(experts, required - 1, 30000)
    with pytest.raises(ValueError):
        runtime.require_ram(True, 22528, 24000)


def test_strata_environment_does_not_inherit_debug_tuning_or_preloads(monkeypatch):
    for key in ("STRATA_DEBUG", "STRATA_DROP_CACHE_ON_EXIT", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "GGML_CUDA_DISABLE_GRAPHS"):
        monkeypatch.setenv(key, "must not leak")
    env = runtime.child_environment(0)
    assert not any(key.startswith("STRATA_") for key in env)
    assert not {"PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "GGML_CUDA_DISABLE_GRAPHS"} & env.keys()
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert env["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] == "0"


def test_memory_scope_must_enforce_budget_and_zero_swap(monkeypatch):
    values = {"cgroup": "0::/test-scope\n", "memory.max": str(22528 * runtime.MIB),
              "memory.current": str(64 * runtime.MIB), "memory.swap.max": "0", "memory.stat": ""}
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: values[path.name])
    runtime.require_memory_scope(22528, 20808)
    for name, wrong in (("memory.max", "max"), ("memory.max", str(24576 * runtime.MIB)),
                        ("memory.swap.max", "max"), ("memory.current", str(20000 * runtime.MIB))):
        previous = values[name]
        values[name] = wrong
        with pytest.raises(ValueError):
            runtime.require_memory_scope(22528, 20808)
        values[name] = previous


def cache_stats(**overrides):
    values = {"anon": 89, "kernel": 1, "file": 2830, "inactive_file": 2830,
              "shmem": 0, "file_mapped": 0, "file_dirty": 0, "file_writeback": 0, "unevictable": 0}
    return '\n'.join(f"{key} {value * runtime.MIB}" for key, value in (values | overrides).items())


def test_verification_cache_does_not_double_count_the_full_model_startup_budget(monkeypatch):
    values = {"cgroup": "0::/test-scope\n", "memory.max": str(22528 * runtime.MIB),
              "memory.current": str(2920 * runtime.MIB), "memory.swap.max": "0", "memory.stat": cache_stats()}
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: values[path.name])
    assert runtime.startup_memory_charge(2920 * runtime.MIB, cache_stats()) == 90 * runtime.MIB
    runtime.require_memory_scope(22528, 20808)
    # Cache credit never authorizes a wider limit or swap.
    for field, invalid in (("memory.max", "max"), ("memory.max", str(24576 * runtime.MIB)), ("memory.swap.max", "1")):
        previous = values[field]
        values[field] = invalid
        with pytest.raises(ValueError):
            runtime.require_memory_scope(22528, 20808)
        values[field] = previous


@pytest.mark.parametrize("field", ["shmem", "file_mapped", "file_dirty", "file_writeback", "unevictable"])
def test_startup_never_credits_nonreclaimable_or_mapped_cache(field):
    assert runtime.startup_memory_charge(2920 * runtime.MIB, cache_stats(**{field: 2830})) == 2920 * runtime.MIB
    # Even if active cache could cover an exclusion, assume it belongs to inactive.
    assert runtime.startup_memory_charge(10000 * runtime.MIB, cache_stats(file=9910, **{field: 2830})) == 10000 * runtime.MIB


@pytest.mark.parametrize("text", ["", "file 100", "file nan", "file -1", "not a counter line",
    cache_stats() + '\nfile 1', cache_stats().replace('kernel 1048576', 'kernel -1')])
def test_unknown_or_malformed_cache_is_charged_in_full(text):
    assert runtime.startup_memory_charge(2920 * runtime.MIB, text) == 2920 * runtime.MIB


def test_cache_credit_is_bounded_by_both_counters_and_preserves_anonymous_kernel_charge():
    assert runtime.startup_memory_charge(2920 * runtime.MIB, cache_stats(file=100)) == 2820 * runtime.MIB
    assert runtime.startup_memory_charge(2920 * runtime.MIB, cache_stats(inactive_file=100)) == 2820 * runtime.MIB
    assert runtime.startup_memory_charge(2920 * runtime.MIB, cache_stats(anon=2900, kernel=20)) == 2920 * runtime.MIB
    assert runtime.startup_memory_charge(100 * runtime.MIB, cache_stats(anon=80, kernel=10)) == 90 * runtime.MIB


def test_startup_samples_charge_again_and_fails_closed_if_cache_statistics_are_unavailable(monkeypatch):
    currents = iter([2920, 23000])
    def read(path, *args, **kwargs):
        if path.name == 'memory.current':
            return str(next(currents) * runtime.MIB)
        return {"cgroup": "0::/test-scope\n", "memory.max": str(22528 * runtime.MIB),
                "memory.swap.max": "0", "memory.stat": cache_stats()}[path.name]
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(ValueError):
        runtime.require_memory_scope(22528, 20808)
    def unavailable(path, *args, **kwargs):
        if path.name == 'memory.stat':
            raise OSError('No statistics')
        return {"cgroup": "0::/test-scope\n", "memory.max": str(22528 * runtime.MIB),
                "memory.current": str(2920 * runtime.MIB), "memory.swap.max": "0"}[path.name]
    monkeypatch.setattr(Path, "read_text", unavailable)
    with pytest.raises(ValueError):
        runtime.require_memory_scope(22528, 20808)


def test_request_policy_rejects_unqualified_features_without_echoing_content():
    request = {"messages": [{"role": "user", "content": "private fixture"}], "max_tokens": 16}
    assert runtime.checked_request(request)["reasoning_effort"] == "none"
    assert "reasoning_effort" not in request
    bad = [{**request, field: value} for field, value in (
        ("tools", []), ("strata_mcp", True), ("response_format", {"type": "json_object"}),
        ("reasoning_effort", "high"), ("max_tokens", 0), ("max_tokens", True),
        ("max_completion_tokens", 16), ("stream", "true"), ("stream_options", {"include_usage": "yes"}),
        ("chat_template_kwargs", {"enable_thinking": True}),
    )]
    bad += [{**request, "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "file:///private fixture"}]}]},
            {**request, "messages": [{"role": "tool", "content": "private fixture"}]}]
    for candidate in bad:
        with pytest.raises(ValueError) as failure:
            runtime.checked_request(candidate)
        assert "private fixture" not in str(failure.value)


def test_engine_failure_is_not_replayed_or_restarted():
    class NonFinite(ValueError):
        pass

    class Died(RuntimeError):
        pass

    class Engine:
        calls = 0

        def generate(self):
            self.calls += 1
            raise NonFinite("private native detail")
            yield 1

    class Service:
        def loaded(self):
            return False

    upstream = SimpleNamespace(StrataEngine=Engine, Service=Service, EngineDied=Died, EngineNonFinite=NonFinite, START_RETRIES=3)
    OwnedEngine, OwnedService = runtime.controlled_classes(upstream)
    engine = OwnedEngine()
    with pytest.raises(Died, match="not replayed"):
        list(engine.generate())
    assert engine.calls == 1 and upstream.START_RETRIES == 0
    with pytest.raises(Died, match="openWeightGym must admit"):
        engine.restart()
    with pytest.raises(Died, match="openWeightGym must admit"):
        OwnedService().ensure_loaded()


@pytest.fixture
def upstream():
    source = os.environ.get("C09_STRATA_SOURCE")
    if not source:
        pytest.skip("Set C09_STRATA_SOURCE to the reviewed disposable checkout for upstream CPU contracts")
    runtime.verify_source(Path(source))
    sys.path[:0] = [source, str(Path(source) / "tools")]
    try:
        yield importlib.import_module("serve.server")
    finally:
        sys.path.remove(source)
        sys.path.remove(str(Path(source) / "tools"))


def test_pinned_upstream_text_http_usage_stream_and_refusals(upstream):
    class Engine(upstream.MockEngine):
        def alive(self):
            return True

    _, Service = runtime.controlled_classes(upstream)
    tokenizer = upstream.ByteTokenizer()
    engine = Engine(tokenizer, ["Hello."])
    service = Service(engine, tokenizer, upstream.ChatTemplate(Path(upstream.ROOT) / "serve/chat_template.jinja"), model_name="strata-fixture")
    key = "fixture-only-not-a-live-key-" + "x" * 32
    server = runtime.http_server(upstream, service, 0, key)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers={"Authorization": "Bearer " + key}, timeout=5) as client:
            assert client.get("/health", headers={"Authorization": ""}).status_code == 401
            health = client.get("/health").json()
            assert health["service"] == "strata" and health["loaded"] is True
            assert client.get("/v1/models").status_code == 200
            for path in ("/", "/settings", "/metrics", "/api-monitor", "/v1/status"):
                assert client.get(path).status_code == 404
            for path in ("/load", "/unload", "/config", "/settings", "/slots/0", "/v1/messages", "/v1/images/generations"):
                assert client.post(path, json={}).status_code == 404
            request = {"model": "strata-fixture", "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 16, "temperature": 0}
            response = client.post("/v1/chat/completions", json=request)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["choices"][0]["message"]["content"] == "Hello."
            assert body["usage"]["prompt_tokens"] > 0 and body["usage"]["completion_tokens"] > 0
            with client.stream("POST", "/v1/chat/completions", json={**request, "stream": True, "stream_options": {"include_usage": True}}) as response:
                assert response.status_code == 200
                lines = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
            assert lines[-1] == "[DONE]"
            assert any(json.loads(line).get("usage") for line in lines[:-1])
            refused = client.post("/v1/chat/completions", json={**request, "strata_mcp": True})
            assert refused.status_code == 400
            assert service.api_monitor is False and not service.api_requests
            assert service.mcp is None and service.slot_save_path is None and service.images is None
    finally:
        server.shutdown()
        server.server_close()


def test_pinned_upstream_nonfinite_failure_is_not_replayed(upstream):
    OwnedEngine, Service = runtime.controlled_classes(upstream)
    # Avoid a native launch: exercise the actual reviewed Service.run with a minimal
    # generator raising the same exception that the native pipe parser raises.
    engine = object.__new__(OwnedEngine)
    calls = []
    original = upstream.StrataEngine.generate
    def fail(*args, **kwargs):
        calls.append(1)
        raise upstream.EngineNonFinite("fixture non-finite output")
        yield 1
    upstream.StrataEngine.generate = fail
    engine.alive = lambda: True
    engine.max_context = 2048
    engine.last = {}
    tokenizer = upstream.ByteTokenizer()
    service = Service(engine, tokenizer, upstream.ChatTemplate(Path(upstream.ROOT) / "serve/chat_template.jinja"))
    try:
        with pytest.raises(upstream.EngineDied, match="not replayed"):
            list(service.run([1, 2], False, None, 16, {}, threading.Event()))
        assert calls == [1]
    finally:
        upstream.StrataEngine.generate = original


def test_pinned_upstream_stream_disconnect_releases_generation(upstream):
    closed = threading.Event()
    class Slow(upstream.MockEngine):
        def alive(self):
            return True

        def generate(self, ids, max_new, sampling, cancel):
            try:
                for token in self.tok.encode("x" * 2000):
                    if cancel.is_set():
                        return
                    yield token
                    time.sleep(.01)
            finally:
                closed.set()

    _, Service = runtime.controlled_classes(upstream)
    tokenizer = upstream.ByteTokenizer()
    engine = Slow(tokenizer, ["unused"])
    service = Service(engine, tokenizer, upstream.ChatTemplate(Path(upstream.ROOT) / "serve/chat_template.jinja"))
    key = "fixture-only-not-a-live-key-" + "x" * 32
    server = runtime.http_server(upstream, service, 0, key)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", headers={"Authorization": "Bearer " + key}, timeout=5) as client:
            with client.stream("POST", "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Fixture."}], "max_tokens": 2048, "stream": True}) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if line.startswith("data: ") and "content" in line:
                        break
            assert closed.wait(3), "Disconnect did not close the owned generation"
            deadline = time.monotonic() + 2
            while service.status.get("busy") and time.monotonic() < deadline:
                time.sleep(.01)
            assert not service.status.get("busy") and not service.status.get("queued")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", ["normal", "nonfinite", "crash", "malformed"])
def test_pinned_native_pipe_failure_and_cleanup(upstream, tmp_path, mode):
    program = tmp_path / "fixture-engine"
    program.write_text(f"#!{sys.executable}\n" + '''
import sys
mode = sys.argv[-1]
print("INFO engine=fixture-only", flush=True)
print("READY 2048 stop", flush=True)
for line in sys.stdin:
    if line.startswith("QUIT"):
        break
    if line.startswith("GEN "):
        if mode == "crash":
            sys.exit(7)
        if mode == "nonfinite":
            print("DONE 0 2 1 1 nonfinite", flush=True)
        elif mode == "malformed":
            # A bad native token must fail, never become a successful empty answer.
            # DONE lets the reviewed parser drain on failure without an untimed wait.
            print("T not-a-token", flush=True)
            print("DONE 0 2 1 1 stop", flush=True)
        else:
            print("T 72", flush=True)
            print("DONE 1 2 1 1 stop", flush=True)
''')
    program.chmod(0o700)
    Engine, _ = runtime.controlled_classes(upstream)
    engine = Engine(str(program), [mode])
    process = engine.proc
    try:
        if mode == "normal":
            assert list(engine.generate([1, 2], 8, {}, threading.Event())) == [72]
            assert engine.last["generated"] == 1
        elif mode == "malformed":
            with pytest.raises(ValueError, match="invalid literal"):
                list(engine.generate([1, 2], 8, {}, threading.Event()))
            with pytest.raises(upstream.EngineDied, match="openWeightGym must admit"):
                engine.restart()
        else:
            with pytest.raises(upstream.EngineDied, match="not replayed" if mode == "nonfinite" else "stopped unexpectedly"):
                list(engine.generate([1, 2], 8, {}, threading.Event()))
            with pytest.raises(upstream.EngineDied, match="openWeightGym must admit"):
                engine.restart()
    finally:
        engine.close()
    assert process.poll() is not None and engine.proc is None
