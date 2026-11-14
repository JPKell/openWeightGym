"""SoundRoom installer, engine, runtime, store and pages without GPU work."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import wave

import pytest
from starlette.testclient import TestClient

from engines import engine_for
from loadcoach.batches import Batches
from soundroom import model
from soundroom.requests import checked_generation
from soundroom.runtime import create_app, native_config, native_request
from soundroom.store import Store
from test_weightroom import desk, sign_in  # noqa: F401  (desk is a fixture)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audiocpp_installer", ROOT / "soundroom/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_optional_install_configuration_is_scoped_private_and_idempotent(tmp_path):
    path = tmp_path / "loadcoach.toml"
    original = 'host = "127.0.0.1"\n[engines.sdcpp]\nenabled = true\nexecutable = "/keep/sd-server"\n[places]\nmodels = "/keep/models"\n'
    path.write_text(original)
    installer.configure(path, Path("/new/audiocpp_server"), Path("/new/audiocpp_cli"), Path("/new/installation.json"))
    parsed = tomllib.loads(path.read_text())
    assert parsed["engines"]["sdcpp"]["executable"] == "/keep/sd-server"
    assert parsed["places"] == {"models": "/keep/models"}
    assert parsed["engines"]["audiocpp"] == {"enabled": True, "executable": "/new/audiocpp_server",
                                             "cli": "/new/audiocpp_cli", "manifest": "/new/installation.json"}
    snapshots = list((tmp_path / "audiocpp-backups").glob("*.before-audiocpp-*"))
    assert len(snapshots) == 1 and snapshots[0].read_text() == original
    assert path.stat().st_mode & 0o777 == 0o600
    installer.configure(path, Path("/new/audiocpp_server"), Path("/new/audiocpp_cli"), Path("/new/installation.json"))
    assert len(list((tmp_path / "audiocpp-backups").glob("*.before-audiocpp-*"))) == 1


def test_dry_run_does_not_create_installation_or_change_settings(tmp_path):
    result = subprocess.run([str(ROOT / "scripts/install-audiocpp.sh"), "--dry-run", "--root", str(tmp_path),
                             "--prefix", str(tmp_path / "native")], text=True, capture_output=True)
    assert result.returncode == 0 and "Would verify/build" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_check_reports_an_optional_missing_runtime(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "soundroom/install.py"), "--check", "--prefix", str(tmp_path)],
                            text=True, capture_output=True)
    assert result.returncode == 0 and "not installed (optional)" in result.stdout


def test_corrupt_release_is_not_adopted_or_executed(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "model_specs").mkdir()
    binary = tmp_path / "bin/audiocpp_server"
    binary.write_bytes(b"#!/bin/sh\necho --config --host --port --backend --device --max-loaded-models\n")
    binary.chmod(0o700)
    (tmp_path / "installation.json").write_text(json.dumps({"executables": {"audiocpp_server": "wrong checksum"}}))
    with pytest.raises(ValueError, match="verification failed"):
        installer.verify(tmp_path)
    (tmp_path / "installation.json").write_text(json.dumps({"executables": {"audiocpp_server": installer.digest(binary)}}))
    assert installer.verify(tmp_path)["executables"]
    (tmp_path / "model_specs").rmdir()
    with pytest.raises(ValueError, match="model_specs"):
        installer.verify(tmp_path)


def wav_bytes(seconds=1.0, rate=48000, channels=2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x10\x00" * int(seconds * rate) * channels)
    return stream.getvalue()


@pytest.fixture
def music(tmp_path):
    package = tmp_path / "ACE-Step1.5-GGUF"
    (package / "turbo").mkdir(parents=True)
    (package / "turbo/ace.gguf").write_bytes(b"weights")
    return {"engine": "audiocpp", "flags": {"model": str(package / "turbo/ace.gguf"), "ctx-size": 0}, "gpus": 1, "alone": True,
            "audiocpp": {"family": "ace_step", "kind": "music", "package": "ace_step_turbo_bf16", "path": str(package / "turbo/ace.gguf"),
                         "files": [], "session_options": {"ace_step.mem_saver": True}}}


@pytest.fixture
def manifest(tmp_path):
    specs = tmp_path / "release/model_specs"
    specs.mkdir(parents=True)
    names = ["route", "lyrics", "duration_sec", "seed", "bpm", "num_inference_steps"]
    (specs / "ace_step.json").write_text(json.dumps({"options": {"request": [{"name": n} for n in names],
                                                                "session": [{"name": "mem_saver"}], "load": []}}))
    return str(tmp_path / "release/installation.json")


def test_audio_profile_is_one_card_or_cpu_and_music_keeps_its_card(music):
    model.check_model("ace", music, ("CUDA0", "CUDA1"))
    model.check_model("ace", {**music, "gpus": ["CUDA1"]}, ("CUDA0", "CUDA1"))
    model.check_model("ace", {**music, "gpus": 0}, ("CUDA0", "CUDA1"))
    assert model.contexts(music) == [0] and model.parallel_capacity(music) == 1
    for bad, why in (({**music, "gpus": 2}, "one GPU"), ({**music, "alone": False}, "exclusively"),
                     ({**music, "audiocpp": {**music["audiocpp"], "kind": "speech"}}, "does not do speech"),
                     ({**music, "audiocpp": {**music["audiocpp"], "family": "../whisper"}}, "valid audio"),
                     ({**music, "audiocpp": {**music["audiocpp"], "files": ["../etc/passwd"]}}, "inside the package"),
                     ({**music, "audiocpp": {**music["audiocpp"], "session_options": {"x": {"nested": 1}}}}, "number, string")):
        with pytest.raises(ValueError, match=why):
            model.check_model("ace", bad, ("CUDA0", "CUDA1"))


def test_runtime_specs_allow_unlisted_families_and_have_no_download_menu(tmp_path, music):
    from soundroom.packages import Packages
    specs = tmp_path/'runtime/model_specs'; specs.mkdir(parents=True)
    (specs/'kokoro_tts.json').write_text(json.dumps({'family':'kokoro_tts','display_name':'Kokoro',
        'tasks':['tts'],'options':{'request':[{'name':'voice_id'}]},'packages':[{'id':'not-downloaded','files':['model.gguf']}]}))
    manifest = str(specs.parent/'installation.json')
    kokoro = {**music,'gpus':0,'alone':False,'audiocpp':{'family':'kokoro_tts','kind':'speech',
        'path':music['flags']['model'],'defaults':{'voice_id':'af_heart'}}}
    assert 'kokoro_tts' not in model.CATALOG
    model.check_model('kokoro',kokoro,())
    assert model.check_options(kokoro,manifest)['family'] == 'kokoro_tts'
    packages = Packages(lambda:{'audio':str(tmp_path)},None,lambda:'',None,lambda:manifest,lambda:set())
    catalogue = packages.catalogue(include_packages=False)
    assert catalogue['families'][0]['family'] == 'kokoro_tts'
    assert catalogue['families'][0]['kinds'] == ['speech'] and catalogue['families'][0]['packages'] == []
    with pytest.raises(ValueError,match='contract is available'):
        model.check_options({**kokoro,'audiocpp':{**kokoro['audiocpp'],'family':'unsupported_family'}},manifest)


def test_audio_file_contract_supplies_family_without_an_installed_package(tmp_path, music):
    from test_weightroom import gguf
    path=gguf(tmp_path/'custom.gguf',general__architecture='audiocpp',audiocpp__model_spec__json=json.dumps({
        'family':'custom_tts','tasks':['tts'],'options':{'request':[{'name':'voice_id'}]}}))
    settings={**music,'flags':{'model':path,'ctx-size':0},'gpus':0,'alone':False,
        'audiocpp':{'family':'custom_tts','kind':'speech','path':path,'defaults':{'voice_id':'default'}}}
    model.check_model('custom',settings,())
    assert model.check_options(settings,'')['family']=='custom_tts'
    assert model.facts(path)['family']=='custom_tts' and model.facts(path)['embedded_contract']
    with pytest.raises(ValueError,match='does not support'):
        model.check_options({**settings,'audiocpp':{**settings['audiocpp'],'kind':'transcription'}},'')


def test_audio_requests_never_enter_chat_and_capabilities_name_the_kind(music):
    engine = engine_for("audiocpp")
    with pytest.raises(ValueError, match="audio routes only"):
        engine.check_request("/v1/chat/completions", {"model": "ace"})
    with pytest.raises(ValueError, match="Unknown"):
        engine.check_request("/v1/audio/generations", {"model": "ace", "native_args": "--evil"})
    capabilities = engine.capabilities(music)
    assert capabilities["music"] and not capabilities["chat"] and not capabilities["speech"]


@pytest.mark.parametrize("extra, why", [
    ({"lyrics": "la", "instrumental": True}, "no lyrics"), ({"mode": "cover"}, "needs source audio"),
    ({"audio": "eA=="}, "continue, cover or repaint"), ({"n": 99}, "takes"), ({"duration": 0}, "duration"),
    ({"mode": "repaint", "audio": "eA==", "repaint_start": 5, "repaint_end": 2}, "after"),
    ({"options": {"route": ["x"]}}, "number, string"), ({"prompt": " "}, "Describe"), ({"seed": 2**40}, "seed"),
])
def test_unsupported_generation_settings_fail_clearly(music, extra, why):
    with pytest.raises(ValueError, match=why):
        checked_generation({"model": "ace", "prompt": "warm synth pop", **extra}, music)


def test_engine_neutral_music_contract_maps_to_each_family(music):
    resolved = checked_generation({"model": "ace", "prompt": "warm synth pop", "lyrics": "[verse]\nhello", "duration": 30}, music)
    request = native_request(resolved, music, 7, None)
    assert request["text"] == "warm synth pop" and request["options"] | {} == {"lyrics": "[verse]\nhello", "duration_sec": 30, "seed": 7, "route": "text2music"}
    instrumental = native_request(checked_generation({"model": "ace", "prompt": "lofi", "instrumental": True}, music), music, 1, None)
    assert instrumental["options"]["lyrics"] == "[Instrumental]"
    repaint = native_request(checked_generation({"model": "ace", "prompt": "x", "mode": "repaint", "audio": "eA==", "repaint_start": 1, "repaint_end": 2}, music), music, 1, b"wav")
    assert repaint["options"]["route"] == "repaint" and repaint["options"]["repainting_end"] == 2 and repaint["audio_base64"] == "d2F2"
    yue = {**music, "audiocpp": {**music["audiocpp"], "family": "yue2"}}
    request = native_request(checked_generation({"model": "y", "prompt": "acoustic pop", "lyrics": "la", "duration": 20}, yue), yue, 3, None)
    assert "text" not in request and request["options"]["style"] == "acoustic pop" and request["options"]["lyrics"] == "la"
    assert request["options"]["semantic_min_tokens"] == request["options"]["semantic_max_tokens"] == 500 and "duration_sec" not in request["options"]
    config = native_config({"settings": music, "cards": []}, 1234)
    assert config["backend"] == "cpu" and config["ui"] is False and config["max_loaded_models"] == 1 and config["host"] == "127.0.0.1"
    assert config["models"][0]["task"] == "gen" and config["models"][0]["session_options"] == {"ace_step.mem_saver": True}


class FakeNative:
    def __init__(self, fail=False):
        self.fail, self.requests, self.stops, self.starts = fail, [], 0, 0

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1

    async def run(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("Fixture generation failure")
        return {"result": {"audio": base64.b64encode(wav_bytes()).decode(), "sample_rate": 48000, "channels": 2}}


def test_unlisted_speech_family_runs_without_curated_metadata(music, manifest):
    class SpeechNative(FakeNative):
        async def call(self, path, **kwargs):
            self.requests.append((path, kwargs))
            return {"audio": base64.b64encode(wav_bytes()).decode()}

    settings = {**music, "audiocpp": {"family": "kokoro_tts", "kind": "speech", "path": music["flags"]["model"]}}
    native = SpeechNative()
    job = {"token": "secret", "settings": settings, "installation": {"manifest": manifest}, "cards": []}
    with TestClient(create_app(job, native)) as client:
        answer = client.post("/v1/audio/speech", headers={"authorization": "Bearer secret"},
                             json={"model": "kokoro", "input": "Hello.", "voice": "af_heart", "response_format": "wav"})
        assert answer.status_code == 200 and answer.content.startswith(b"RIFF")
        assert native.requests[0][0] == "/v1/audio/speech"
        assert native.requests[0][1]["json"]["voice"] == "af_heart"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg measures generated audio")
def test_private_audio_runtime_streams_takes_with_seeds_and_metrics(music, manifest):
    native = FakeNative()
    job = {"token": "secret", "settings": music, "installation": {"manifest": manifest}, "cards": ["CUDA0"]}
    with TestClient(create_app(job, native)) as client:
        assert client.get("/health").status_code == 401
        headers = {"authorization": "Bearer secret"}
        answer = client.post("/v1/audio/generations", headers=headers, json={"model": "ace", "prompt": "warm synth pop", "seed": 17, "n": 2, "stream": True})
        assert answer.status_code == 200 and "event: error" not in answer.text
        assert answer.text.count("event: audio_generation.completed") == 2
        assert [one["options"]["seed"] for one in native.requests] == [17, 18]
        events = [json.loads(line[6:]) for line in answer.text.splitlines() if line.startswith("data: ")]
        measured = events[-1]["metrics"]
        assert measured["rate"] == 48000 and measured["channels"] == 2 and abs(measured["duration"] - 1) < .05
        refused = client.post("/v1/audio/generations", headers=headers, json={"model": "ace", "prompt": "x", "options": {"not_declared": 1}})
        assert refused.status_code == 400 and "declares no request option" in refused.json()["error"]["message"]


def test_generation_failure_stops_native_before_releasing_private_slot(music, manifest):
    native = FakeNative(fail=True)
    job = {"token": "secret", "settings": music, "installation": {"manifest": manifest}, "cards": ["CUDA0"]}
    with TestClient(create_app(job, native)) as client:
        answer = client.post("/v1/audio/generations", headers={"authorization": "Bearer secret"}, json={"model": "ace", "prompt": "x"})
        assert answer.status_code == 502 and "Fixture generation failure" in answer.json()["error"]["message"]
        assert native.stops >= 1
        assert client.post("/v1/audio/generations", headers={"authorization": "Bearer secret"}, json={"model": "ace", "prompt": "x"}).status_code == 502


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg folds the loop seam")
def test_seamless_sound_loop_asks_for_the_fade_and_keeps_the_requested_length(music):
    import asyncio
    from soundroom.runtime import fade_of, metrics, seamless
    sound = {**music, "audiocpp": {**music["audiocpp"], "family": "stable_audio", "kind": "sound"}}
    with pytest.raises(ValueError, match="seamless loop"):
        checked_generation({"model": "ace", "prompt": "rain", "duration": 8, "loop": True}, music)
    with pytest.raises(ValueError, match="seamless loop"):
        checked_generation({"model": "s", "prompt": "rain", "loop": True}, sound)
    resolved = checked_generation({"model": "s", "prompt": "rain", "duration": 8, "loop": True}, sound)
    assert native_request(resolved, sound, 1, None)["options"]["duration_sec"] == 8 + fade_of(8) == 9
    folded = asyncio.run(seamless(wav_bytes(seconds=9), 8, fade_of(8)))
    assert asyncio.run(metrics(folded))["duration"] == 8


def test_spoken_profiles_need_their_voice_source(music):
    from soundroom.requests import checked_script, checked_speech
    clone = {**music, "audiocpp": {**music["audiocpp"], "family": "qwen3_tts", "kind": "clone"}}
    design = {**music, "audiocpp": {**music["audiocpp"], "family": "qwen3_tts", "kind": "design"}}
    with pytest.raises(ValueError, match="reference recording"):
        checked_speech({"model": "q", "input": "Hello"}, clone)
    with pytest.raises(ValueError, match="description of the voice"):
        checked_speech({"model": "q", "input": "Hello"}, design)
    with pytest.raises(ValueError, match="Only a cloning profile"):
        checked_speech({"model": "q", "input": "Hello", "voice_audio": "eA==", "instructions": "warm"}, design)
    assert checked_speech({"model": "q", "input": "Hello", "voice_audio": "eA==", "voice_text": "Hi"}, clone)["format"] == "mp3"
    script = checked_script({"model": "q", "script": [{"text": "One.", "voice_audio": "eA=="}, {"text": "Two.", "voice_audio": "eA=="}]}, clone)
    assert [line["text"] for line in script["lines"]] == ["One.", "Two."] and script["gap"] == .3
    from soundroom.model import CATALOG
    from soundroom.runtime import speech_request
    assert speech_request({"text": "Hello."}, {}, 1, None, CATALOG["vibevoice"])["input"] == "Speaker 1: Hello."
    assert speech_request({"text": "Speaker 2: Hi."}, {}, 1, None, CATALOG["vibevoice"])["input"] == "Speaker 2: Hi."
    assert speech_request({"text": "Hello."}, {}, 1, None, CATALOG["qwen3_tts"])["input"] == "Hello."


def test_transcripts_take_openai_shapes_from_timed_words():
    from soundroom.runtime import transcript
    value = {"text": "Hello there. Bye.", "sample_rate": 16000, "timing": {"audio_duration_ms": 4000},
             "words": [{"word": "Hello", "start_sample": 0, "end_sample": 8000}, {"word": "there.", "start_sample": 8000, "end_sample": 16000},
                       {"word": "Bye.", "start_sample": 40000, "end_sample": 48000}]}
    verbose = json.loads(transcript(value, "verbose_json").body)
    assert [one["text"] for one in verbose["segments"]] == ["Hello there.", "Bye."] and verbose["duration"] == 4
    assert verbose["words"][2] == {"word": "Bye.", "start": 2.5, "end": 3.0}
    assert transcript(value, "srt").body.decode().startswith("1\n00:00:00,000 --> 00:00:01,000\nHello there.\n\n2\n00:00:02,500")
    assert transcript(value, "vtt").body.decode().startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.000")
    assert transcript(value, "text").body == b"Hello there. Bye."


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe checks stored audio")
def test_audio_assets_are_private_shareable_by_group_and_temporary_audio_stays_in_memory(tmp_path):
    batches = Batches(str(tmp_path / "accounts.db"))
    store = Store(batches.db, tmp_path / "data")
    with pytest.raises(ValueError, match="not an audio file"):
        store.save("owner", b"not audio at all")
    one = store.save("owner", wav_bytes(), kind="take", metadata={"settings": {"prompt": "x"}})
    assert one["seconds"] == 1 and one["rate"] == 48000 and one["channels"] == 2
    assert store.path(one["id"]).stat().st_mode & 0o777 == 0o600
    assert store.content("other", one["id"], "plain") is None
    store.share("owner", one["id"], ["plain"], {"plain", "admin"})
    shared = store.asset("other", one["id"], "plain")
    assert shared and not shared["mine"] and shared["shared_with"] == [] and store.content("other", one["id"], "plain") == wav_bytes()
    assert not store.delete("other", one["id"])
    with pytest.raises(ValueError, match="existing groups"):
        store.share("owner", one["id"], ["nobody"], {"plain"})
    with pytest.raises(ValueError, match="retained audio of yours"):
        store.update("other", one["id"], {"favourite": True})
    store.update("owner", one["id"], {"favourite": True, "tags": ["drums", "demo"]})
    assert [row["id"] for row in store.listing("owner", tag="drums")] == [one["id"]]
    assert [row["id"] for row in store.listing("owner", favourites=True)] == [one["id"]]
    assert [row["id"] for row in store.listing("other", "plain")] == [one["id"]]
    quiet = store.save("owner", wav_bytes(), kind="take", temporary=True)
    assert not store.path(quiet["id"]).exists() and store.content("owner", quiet["id"]) == wav_bytes()
    assert not store.db.execute("SELECT 1 FROM soundroom_assets WHERE id=?", (quiet["id"],)).fetchone()


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe checks stored audio")
def test_a_job_whose_every_take_failed_says_failed_and_why(tmp_path):
    from soundroom.front import outcome
    batches = Batches(str(tmp_path / "accounts.db"))
    lines = [{"custom_id": "script", "body": {"model": "vibevoice"}}]
    batch = batches.start("jordan", "/v1/audio/generations", "file-x", lines)
    batches.keep(batch["id"], 0, 502, "", {"error": {"message": "VibeVoice prompt has no valid Speaker N: lines"}})
    assert outcome(batches, batches.finish(batch["id"])) == ("failed", "VibeVoice prompt has no valid Speaker N: lines")
    good = batches.start("jordan", "/v1/audio/generations", "file-y", lines * 2)
    batches.keep(good["id"], 0, 200, "", {"data": []})
    batches.keep(good["id"], 1, 502, "", {"error": {"message": "out of memory"}})
    assert outcome(batches, batches.finish(good["id"])) == ("completed", "out of memory")
    assert outcome(batches, None) == ("completed", None)


def test_saved_voices_keep_their_recording_and_follow_sharing(tmp_path):
    batches = Batches(str(tmp_path / "accounts.db"))
    store = Store(batches.db, tmp_path / "data")
    clip = store.save("owner", wav_bytes(), kind="recording")
    voice = store.save_voice("owner", "Narrator", clip["id"], "Hello there.", consent=1.0)
    with pytest.raises(ValueError, match="saved voice"):
        store.delete("owner", clip["id"])
    assert store.voice_reference("other", voice["id"], "plain") is None
    store.share("owner", voice["id"], ["plain"], {"plain"})
    assert store.voice_reference("other", voice["id"], "plain") == (wav_bytes(), "Hello there.")
    assert not store.delete_voice("other", voice["id"])
    assert store.delete_voice("owner", voice["id"]) and store.delete("owner", clip["id"])


def test_audio_capture_keeps_settings_but_never_audio_bytes():
    from soundroom import stream
    value = {"type": "audio_generation.completed", "b64_wav": "AUDIO-BYTES", "settings": {"prompt": "rain"}}
    capture = stream.Capture("text/event-stream")
    encoded = stream.encode(value)
    for index in range(0, len(encoded), 5):
        capture.feed(encoded[index:index + 5])
    assert capture.outcome() == "success" and b"AUDIO-BYTES" not in capture.snapshot() and stream.completed(capture.snapshot())
    assert stream.metadata({"model": "m", "audio": "x", "prompt": "p"}) == {"model": "m", "prompt": "p"}


def test_multipart_transcription_uploads_become_base64_json():
    from soundroom.requests import request_body
    boundary = "XyZ"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nparakeet\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + b"RIFFdata" + \
           (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"timestamp_granularities[]\"\r\n\r\nword\r\n--{boundary}--\r\n").encode()
    value = request_body(f"multipart/form-data; boundary={boundary}", body)
    assert value == {"model": "parakeet", "file": base64.b64encode(b"RIFFdata").decode(), "timestamp_granularities": ["word"]}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg converts downloads")
def test_audio_pages_assets_sharing_and_voices_require_identity_origin_and_consent(desk):
    browser, switch, users = desk
    assert browser.get("/chat/music", follow_redirects=False).status_code == 303
    sign_in(browser, "jordan")
    for path, title in (("/chat/music", "Music"), ("/chat/sounds", "Sounds"), ("/chat/speech", "Voice generation"), ("/chat/transcribe", "Transcribe"), ("/models/audio", "Audio models"),
                        ("/account/audio", "Download format"), ("/system/audio", "Audio settings")):
        answer = browser.get(path)
        assert answer.status_code == 200 and title in answer.text, path
    assert browser.get("/soundroom-static/music.js").status_code == 200
    # Reported defaults are editable and apply to the next page request in this app.
    saved = browser.post("/system/audio", data={"value-audio_lyrics_model": "writer-model"})
    assert saved.status_code == 200 and "Applied immediately." in saved.text and 'name="value-audio_lyrics_model"' in saved.text
    assert browser.post("/soundroom-api/preferences", json={"format": "mp3"}).json()["format"] == "mp3"
    assert browser.post("/soundroom-api/preferences", json={"format": "exe"}).status_code == 400
    state = browser.get("/soundroom-api/state").json()
    assert state["models"] == [] and "plain" in state["groups"]
    refused = browser.post("/soundroom-api/jobs", json={"request": {"model": "nothing", "prompt": "rain", "n": 1}})
    assert refused.status_code == 400 and "music, sound or speech model" in refused.json()["error"]["message"]
    created = browser.post("/soundroom-api/assets", json={"b64_audio": base64.b64encode(wav_bytes()).decode(), "filename": "me.wav", "kind": "recording"})
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    content_path = f'/soundroom-api/assets/{identifier}/content'
    whole = browser.get(content_path)
    ranged = browser.get(content_path, headers={'range': 'bytes=44-99'})
    assert ranged.status_code == 206 and ranged.content == whole.content[44:100]
    assert ranged.headers['content-range'] == f'bytes 44-99/{len(whole.content)}'
    assert ranged.headers['accept-ranges'] == 'bytes' and ranged.headers['cache-control'] == 'no-store'
    assert browser.get(content_path, headers={'range': 'bytes=-16'}).content == whole.content[-16:]
    assert browser.get(content_path, headers={'range': 'bytes=9999999-'}).status_code == 416
    headed = browser.head(content_path)
    assert headed.status_code == 200 and not headed.content and int(headed.headers['content-length']) == len(whole.content)
    flac = browser.get(f"/soundroom-api/assets/{identifier}/content?format=flac&download=1")
    assert flac.status_code == 200 and flac.content.startswith(b"fLaC") and "me.flac" in flac.headers["content-disposition"]
    no = browser.post("/soundroom-api/voices", json={"name": "Me", "asset": identifier, "transcript": "hi"})
    assert no.status_code == 400 and "permission" in no.json()["error"]["message"]
    voice = browser.post("/soundroom-api/voices", json={"name": "Me", "asset": identifier, "transcript": "hi", "consent": True})
    assert voice.status_code == 201 and voice.json()["consent"]
    assert browser.delete(f"/soundroom-api/assets/{identifier}", headers={"origin": "https://elsewhere.example"}).status_code == 403
    assert browser.post(f"/soundroom-api/assets/{identifier}", json={"groups": ["plain"]}).json()["shared_with"] == ["plain"]
    sign_in(browser, "pat")
    assert browser.get(f"/soundroom-api/assets/{identifier}/content").status_code == 200
    assert browser.delete(f"/soundroom-api/assets/{identifier}").status_code == 403
    assert browser.get("/models/audio").status_code == 403
    sign_in(browser, "wanda")  # another group, not shared with
    assert browser.get(f"/soundroom-api/assets/{identifier}/content").status_code == 404
    assert browser.get(content_path, headers={'range': 'bytes=0-99'}).status_code == 404


def test_voice_service_speech_waits_for_a_pause_then_transcribes_once_and_speaks_in_the_chosen_voice():
    import asyncio
    import struct
    from soundroom.voice import AudioSpeech
    asked = []

    async def request(method, path, body):
        asked.append((path, body))
        return b'{"text": " hello there "}' if path.endswith("transcriptions") else b"PCM"
    heard = AudioSpeech({"recognition_model": "parakeet", "synthesis_model": "qwen3-clone", "trailing_silence_ms": 300}, request)
    loud, quiet = struct.pack("<1600h", *([3000, -3000] * 800)), bytes(3200)

    async def run():
        stream = heard.stream()
        assert await heard.hear(stream, quiet) == ("", None) and await heard.hear(stream, loud) == ("", None)
        assert await heard.hear(stream, quiet) == ("", None) and not asked
        assert await heard.hear(stream, quiet) == ("", None)
        assert await heard.hear(stream, quiet) == ("hello there", "hello there")
        assert await heard.hear(stream, quiet, finish=True) == ("", "")  # silence alone is never a turn
        assert await heard.say("Hi.", 3, voice={"audio": "UklGRg==", "text": "words"}) == b"PCM"
    asyncio.run(run())
    assert [path for path, _ in asked] == ["/v1/audio/transcriptions", "/v1/audio/speech"]
    assert asked[0][1]["model"] == "parakeet" and base64.b64decode(asked[0][1]["file"]).startswith(b"RIFF")
    assert asked[1][1] == {"model": "qwen3-clone", "input": "Hi.", "response_format": "pcm", "voice_audio": "UklGRg==", "voice_text": "words"}
    assert heard.models() == {"parakeet", "qwen3-clone"}


def test_voice_speaking_speed_changes_pcm_duration_without_native_speed_support():
    """Cloned models share pitch-preserving tempo control even without a native speed option."""
    import asyncio
    import math
    import struct
    from soundroom.voice import AudioSpeech

    pcm = struct.pack('<24000h', *(int(5000 * math.sin(2 * math.pi * 440 * n / 24000)) for n in range(24000)))
    async def request(method, path, body):
        assert 'speed' not in body and 'options' not in body
        return pcm
    heard = AudioSpeech({'synthesis_model': 'clone'}, request)
    faster = asyncio.run(heard.say('A sample.', speed=1.5))
    assert .6 < len(faster) / len(pcm) < .72


def test_library_pages_cover_equal_timestamps_and_filter_tags_before_the_limit(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from urllib.parse import urlencode

    from starlette.requests import Request

    from soundroom.front import Front

    batches = Batches(str(tmp_path / "accounts.db"))
    store = Store(batches.db, tmp_path / "data")
    with store.db:
        for index in range(205):
            store.db.execute("INSERT INTO soundroom_assets (id,owner_id,kind,filename,sha256,seconds,rate,channels,bytes,created,metadata,tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"audio_{index:032x}", "owner", "take", f"take-{index}.wav", "hash", 1, 48000, 2, 100, 1,
                              "{}", json.dumps(["old_demo"] if index == 0 else ["oldXdemo"])))
    front = Front(store, SimpleNamespace(store=batches), SimpleNamespace(groups={"plain"}))

    async def catalogue(user):
        return []
    front.catalogue = catalogue
    user = SimpleNamespace(identity="owner", group_name="plain", can=lambda ability: False)

    def page(params):
        request = Request({"type": "http", "query_string": urlencode(params).encode(), "headers": []})
        return asyncio.run(front.state(request, user))

    first = json.loads(page({"kinds": "take"}).body)
    second = json.loads(page({"kinds": "take", **first["next"]}).body)
    third = json.loads(page({"kinds": "take", **second["next"]}).body)
    identifiers = [row["id"] for part in (first, second, third) for row in part["assets"]]
    assert len(identifiers) == len(set(identifiers)) == 205
    assert third["next"] is None
    found = json.loads(page({"kinds": "take", "tag": "old_demo"}).body)
    assert [row["id"] for row in found["assets"]] == ["audio_" + "0" * 32]
    assert json.loads(page({"kinds": "take", "tag": "OLD_demo"}).body)["assets"] == []
    for params in ({"before": "nan"}, {"before": "inf"}, {"before_id": identifiers[0]}, {"before": 1, "before_id": "invalid"}):
        assert page(params).status_code == 400


def test_reuse_recovers_original_voices_and_source_without_exposing_another_owners_job(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from starlette.requests import Request

    from soundroom.front import Front

    batches = Batches(str(tmp_path / "accounts.db"))
    store = Store(batches.db, tmp_path / "data")
    source = store.save("owner", wav_bytes(), kind="reference")
    payload = {"request": {"model": "clone", "script": [{"text": "Hello", "voice_id": "voice_one"}, {"text": "Bye", "voice_id": "voice_two"}],
                           "gap": .4, "options": {"speed": 1.2}}, "source": source["id"]}
    batch = batches.start("jordan", "/v1/audio/generations", "file-x", [{"custom_id": "script", "body": {"model": "clone"}}])
    store.bind_job(batch["id"], "owner", "model-id", "clone", payload, [source["id"]])
    take = store.save("owner", wav_bytes(), kind="speech", metadata={"job": batch["id"], "model": "clone", "seed": 17,
                     "settings": {"lines": [{"text": "Hello"}, {"text": "Bye"}], "options": {"temperature": .8}}})
    store.share("owner", take["id"], ["plain"], {"plain"})
    front = Front(store, SimpleNamespace(store=batches), SimpleNamespace(groups={"plain"}))

    async def catalogue(user):
        return []
    front.catalogue = catalogue

    def state(owner):
        user = SimpleNamespace(identity=owner, group_name="plain", can=lambda ability: False)
        request = Request({"type": "http", "query_string": b"kinds=speech", "headers": []})
        return json.loads(asyncio.run(front.state(request, user)).body)["assets"][0]["reuse"]

    owned = state("owner")
    assert owned["request"]["script"] == payload["request"]["script"]
    assert owned["request"]["seed"] == 17 and owned["source"] == source["id"]
    assert owned["request"]["options"] == {"temperature": .8, "speed": 1.2}
    shared = state("other")
    assert "script" not in shared["request"] and shared["source"] is None


def test_failed_job_stays_failed_when_its_error_file_is_missing(tmp_path):
    from soundroom.front import outcome
    batches = Batches(str(tmp_path / "accounts.db"))
    batch = batches.start("jordan", "/v1/audio/generations", "file-x", [{"custom_id": "take", "body": {"model": "heartmula"}}])
    batches.keep(batch["id"], 0, 502, "", {"error": {"message": "Lyrics are required"}})
    finished = batches.finish(batch["id"])
    finished["error_file_id"] = None
    status, message = outcome(batches, finished)
    assert status == "failed" and "no detailed error" in message.lower()


def test_heartmula_requires_lyrics_before_native_inference(music):
    settings = {**music, "audiocpp": {**music["audiocpp"], "family": "heartmula"}}
    for extra in ({}, {"instrumental": True}, {"lyrics": "   "}):
        with pytest.raises(ValueError, match="needs lyrics"):
            checked_generation({"prompt": "pop, piano", **extra}, settings)
    assert checked_generation({"prompt": "pop, piano", "lyrics": "[verse]\nHello"}, settings)["lyrics"]


def test_lyrics_helper_uses_duration_and_instrumental_choice():
    import asyncio
    from types import SimpleNamespace

    import httpx
    from starlette.requests import Request

    from soundroom.front import Front
    sent = []

    async def post(path, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"caption":"warm acoustic instrumental","lyrics":""}'}}]})

    async def run():
        front = Front(None, SimpleNamespace(door=SimpleNamespace(post=post), internal_key=lambda user: "fixture"), None)
        async def receive():
            return {"type": "http.request", "body": json.dumps({"idea": "warm acoustic intro", "duration": 12, "instrumental": True}).encode()}
        request = Request({"type": "http", "headers": []}, receive)
        result = await front.lyrics(request, SimpleNamespace(name="owner"))
        assert result.status_code == 200
    asyncio.run(run())
    prompt = sent[0]["messages"][0]["content"]
    assert "12 seconds" in prompt and "lyrics must be empty" in prompt


def test_collections_and_workflows_are_benchmark_pages_used_to_compare_models():
    from weightroom.navigation import navigation
    chat = navigation("/chat", [], False)
    assert chat["section"] == "Interact" and chat["links"][0] == ("Text chat", "/chat")
    assert not {"/collections", "/workflows"} & {url for _, url in chat["links"]}
    for path, active in (("/collections", "/collections"), ("/collections/example", "/collections"),
                         ("/collections/example/sources/3", "/collections"), ("/workflows", "/workflows"),
                         ("/workflows/example", "/workflows")):
        result = navigation(path, [], False)
        assert result["section"] == "Benchmark" and result["active"] == active
        assert result["links"][-2:] == [("Collections", "/collections"), ("Workflows", "/workflows")]
    # Manage stays last for those who may use it.
    assert navigation("/collections", ["manage-settings"], False)["links"][-3:] == [
        ("Collections", "/collections"), ("Workflows", "/workflows"), ("Manage", "/benchmarks/manage")]


def test_a_spoken_exchange_is_kept_as_a_conversation_without_asking_a_model(desk):
    browser, switch, users = desk
    key = {"authorization": "Bearer " + users.new_key("jordan")}
    first = browser.post("/loadcoach/conversations/spoken", headers=key, json={"said": "Hello?", "answered": "Hi there.", "model": "m", "voice": "voice_x"})
    assert first.status_code == 201
    again = browser.post("/loadcoach/conversations/spoken", headers=key, json={"conversation": first.json()["id"], "said": "Bye.", "answered": "Goodbye.", "model": "m"})
    assert again.json()["id"] == first.json()["id"]
    talk = browser.get("/loadcoach/conversations/" + first.json()["id"], headers=key).json()
    assert talk["title"] == "Spoken: Hello?" and [(m["role"], m["content"]) for m in talk["messages"]] == [("user", "Hello?"), ("assistant", "Hi there."), ("user", "Bye."), ("assistant", "Goodbye.")]
    other = {"authorization": "Bearer " + users.new_key("pat")}
    assert browser.post("/loadcoach/conversations/spoken", headers=other, json={"conversation": first.json()["id"], "said": "x"}).status_code == 404


def test_a_package_given_by_its_main_gguf_lists_its_other_files_from_its_folder(music, tmp_path):
    yue = {**music, "audiocpp": {**music["audiocpp"], "family": "yue2", "path": str(tmp_path / "Yue2/yue2-3b-q8_0.gguf"),
                                 "files": ["yue2-vae-f16.gguf", "sidecars/yue2-model-config.json"]}}
    assert model.files_of(yue)["package:sidecars/yue2-model-config.json"] == str(tmp_path / "Yue2/sidecars/yue2-model-config.json")


def test_future_audio_family_uses_declared_mode_fields_without_new_runtime_branches(monkeypatch):
    from soundroom import model, runtime
    monkeypatch.setitem(model.CATALOG, "future_audio", {
        "caption": "description", "mode_option": "operation", "mode_values": {"repaint": "patch"},
        "repaint_start_option": "begin", "repaint_end_option": "end", "repaint_as_text": True})
    result = runtime.native_request({"prompt": "test", "options": {}, "instrumental": True, "lyrics": "",
                                    "mode": "repaint", "repaint_start": 1.5, "repaint_end": 3.0},
                                   {"audiocpp": {"family": "future_audio"}}, 42, None)
    assert result["options"] == {"description": "test", "seed": 42, "operation": "patch", "begin": "1.5", "end": "3.0"}
