"""Hermes final-output, conversation isolation and uncertain-outcome boundaries."""

import asyncio
import json

import httpx
import pytest

from voice.hermes import Bridge, configuration


def test_only_completed_output_and_confirmed_delivery_enter_history():
    async def check():
        submitted = []

        async def api(request):
            if request.method == "POST":
                submitted.append(json.loads(request.content))
                return httpx.Response(200, json={"run_id": "run-one"})
            return httpx.Response(200, json={"status": "completed", "output": "Final answer.",
                                            "reasoning": "Never spoken", "tools": ["Never spoken"]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
            first = Bridge(http, dict(url="http://hermes", model="small"), "first")
            second = Bridge(http, dict(url="http://hermes", model="small"), "second")
            assert await first.answer("Question", None, 1) == "Final answer."
            assert first.heard_history() == [{"role": "user", "content": "Question"}]
            assert "Never read internal playback status" in submitted[0]["instructions"]
            assert "Do not repeat tool actions" in submitted[0]["instructions"]
            first.delivery[1] = ["Final"]
            assert first.heard_history()[-1]["content"] == "Final"
            assert not second.heard_history()
            assert submitted[0]["conversation_history"] == []
            assert submitted[0]["session_id"] == "first"
    asyncio.run(check())


def test_unknown_submission_is_not_retried():
    async def check():
        submitted = []

        async def api(request):
            submitted.append(request)
            raise httpx.ReadTimeout("Response was lost", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
            bridge = Bridge(http, dict(url="http://hermes", model="small"), "one")
            with pytest.raises(httpx.ReadTimeout):
                await bridge.answer("Run once", None)
            with pytest.raises(ValueError, match="outcome is unknown"):
                await bridge.answer("Try again", None)
            assert len(submitted) == 1
    asyncio.run(check())


def test_cancellation_waits_for_stop_confirmation():
    async def check():
        started = asyncio.Event()
        stopped = False

        async def api(request):
            nonlocal stopped
            if request.url.path.endswith("/stop"):
                stopped = True
                return httpx.Response(200, json={})
            if request.method == "POST":
                return httpx.Response(200, json={"run_id": "run-one"})
            started.set()
            return httpx.Response(200, json={"status": "cancelled" if stopped else "running"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
            bridge = Bridge(http, dict(url="http://hermes", model="small"), "one")
            task = asyncio.create_task(bridge.answer("Search", None))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stopped and not bridge.uncertain and bridge.run_id is None
    asyncio.run(check())


def test_gateway_choices_reach_isolated_config_and_run_without_changing_an_active_call(tmp_path, monkeypatch):
    """The independent Hermes controls affect both process defaults and each submitted run."""
    key = tmp_path / "provider.key"
    key.write_text("test-key")
    gateway = dict(hermes=dict(model="original", python="python", repository=str(tmp_path),
                              provider_url="http://coach/v1", provider_key_file=str(key)),
                   hermes_model="chosen", hermes_system_prompt="Speak as Jeeves.", hermes_max_turns=4,
                   hermes_context_tokens=4096, hermes_max_output_tokens=256, hermes_reasoning_effort="low")
    selected = configuration(gateway)
    gateway["hermes_model"] = "next-call"
    assert selected["model"] == "chosen"
    assert configuration({"hermes": gateway["hermes"], "hermes_model": ""})["model"] == "original"
    submitted = []

    class Process:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        from pathlib import Path
        config = json.loads((Path(kwargs["env"]["HERMES_HOME"]) / "config.yaml").read_text())
        assert config["model"]["default"] == "chosen"
        assert config["model"]["max_tokens"] == 256
        assert config["agent"] == dict(max_turns=4, context_tokens=4096, reasoning_effort="low")
        assert config["platform_toolsets"] == {"api_server": ["search"]}
        return Process()

    async def api(request):
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"features": dict(run_submission=True, run_status=True, run_stop=True)})
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={"run_id": "one"})
        return httpx.Response(200, json={"status": "completed", "output": "Yes, sir."})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
            bridge = Bridge(http, selected, "one")
            try:
                await bridge.ready()
                assert await bridge.answer("Hello", None) == "Yes, sir."
                assert submitted[0]["model"] == "chosen"
                assert submitted[0]["reasoning_effort"] == "low"
                assert submitted[0]["instructions"].startswith("Speak as Jeeves.")
                assert "only assistant speech delivered to the caller" in submitted[0]["instructions"]
            finally:
                await bridge.close()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    asyncio.run(check())
