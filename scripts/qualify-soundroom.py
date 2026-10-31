#!/usr/bin/env python3
"""Qualify SoundRoom end to end on this compute host, from this checkout, under the external-work lock.

It runs this checkout's LoadCoach (on the real cards), Turnstile and WeightRoom in one process,
inside a transient systemd service with delegated memory, while the installed LoadCoach is
locked and its idle models unloaded. Nothing touches installed settings, accounts or data: the
stack has its own accounts and databases in the new output directory. The scenario drives the
pages' own APIs and a headless browser, and writes report.json with every step.

  scripts/qualify-soundroom.py --output data/qualification/soundroom-NEW \\
      --suite-key-file ~/ai/igen/.llm-api-key --models ~/ai/models/audio [--skip music,families,speech]

Every family in PACKAGES whose files are in the audio place is qualified; Parakeet is required, and
ACE-Step unless music is skipped. Takes are judged by metrics only: length, format, level and
silence; spoken output is transcribed back (word error rate), cloned voices are compared with
their reference by speaker embedding, and every transcription family hears the same recordings.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PORT = 18440
# name: family, kind, package file (relative to the audio place), extra audiocpp settings, request
PACKAGES = {
    "ace-turbo": ("ace_step", "music", "ACE-Step1.5-GGUF/turbo/ace-step-1.5-turbo-bf16.gguf", {}, None),
    "parakeet": ("parakeet_tdt", "transcription", "Parakeet-TDT-0.6B-v3-GGUF/parakeet-tdt-0.6b-v3-q8_0.gguf", {}, None),
    "qwen3-clone": ("qwen3_tts", "clone", "Qwen3-TTS-12Hz-1.7B-Base-GGUF/qwen3-tts-12hz-1.7b-base-q8_0_v2.gguf", {}, None),
    "qwen3-design": ("qwen3_tts", "design", "Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF/qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf", {}, None),
    "sortformer": ("sortformer_diar", "diarization", "Sortformer-Diar-4spk-v1-GGUF/sortformer-diar-4spk-v1-q8_0.gguf", {}, None),
    "stable-music": ("stable_audio", "music", "Stable-Audio-3-Small-Music-GGUF/stable-audio-3-small-music-q8_0.gguf", {},
                     {"prompt": "uplifting house music with bright synths and festival drums", "duration": 15, "seed": 11}),
    "heartmula": ("heartmula", "music", "HeartMuLa-GGUF/heartmula-q8_0.gguf", {},
                  {"prompt": "pop, piano, warm female vocal", "lyrics": "[verse]\nCity lights are shining bright\n[chorus]\nHold on tonight", "duration": 20, "seed": 11}),
    "yue2": ("yue2", "music", "Yue2-3B-GGUF/yue2-3b-q8_0.gguf", {"files": ["yue2-3b-q8_0.gguf", "yue2-vae-f16.gguf", "sidecars/yue2-qwen.tiktoken",
                                                          "sidecars/yue2-model-config.json", "sidecars/yue2-vae-config.json", "sidecars/yue2-generation-config.json"],
                                                "session_options": {"yue2.model_gguf": "yue2-3b-q8_0.gguf", "yue2.vae_gguf": "yue2-vae-f16.gguf"},
                                                "defaults": {"cot": "off"}},
             {"prompt": "English, acoustic pop, warm vocals", "lyrics": "[verse]\nMorning sunlight fills the sky\n[chorus]\nSing together, you and I", "duration": 20, "seed": 11}),
    "midasheng-music": ("midashenglm_gen", "music", "MiDashengLM-Gen-GGUF/midashenglm-gen-q8_0.gguf", {},
                        {"prompt": "calm lo-fi beat with soft piano", "duration": 10, "seed": 11}),
    "stable-sfx": ("stable_audio", "sound", "Stable-Audio-3-Small-SFX-GGUF/stable-audio-3-small-sfx-q8_0.gguf", {},
                   {"prompt": "rain on a tin roof, distant thunder", "duration": 8, "seed": 11, "loop": True}),
    "midasheng-sfx": ("midashenglm_gen", "sound", "MiDashengLM-Gen-GGUF/midashenglm-gen-q8_0.gguf", {},
                      {"prompt": "a door creaks open and closes", "duration": 5, "seed": 11}),
    "qwen3-clone-small": ("qwen3_tts", "clone", "Qwen3-TTS-12Hz-0.6B-Base-GGUF/qwen3-tts-12hz-0.6b-base-q8_0.gguf", {}, None),
    "chatterbox": ("chatterbox", "clone", "Chatterbox-GGUF/chatterbox-q8_0.gguf", {}, None),
    "chatterbox-turbo": ("chatterbox_turbo", "speech", "Chatterbox-Turbo-GGUF/chatterbox-turbo-q8_0.gguf", {}, None),
    "voxcpm2-clone": ("voxcpm2", "clone", "VoxCPM2-GGUF/voxcpm2-q8_0.gguf", {}, None),
    "voxcpm2-design": ("voxcpm2", "design", "VoxCPM2-GGUF/voxcpm2-q8_0.gguf", {}, None),
    "index-tts2": ("index_tts2", "clone", "IndexTTS2-GGUF/index-tts2-q8_0.gguf", {}, None),
    "vibevoice": ("vibevoice", "speech", "VibeVoice-1.5B-GGUF/vibevoice-1.5b-q8_0.gguf", {}, None),
    "qwen3-asr": ("qwen3_asr", "transcription", "Qwen3-ASR-1.7B-GGUF/qwen3-asr-1.7b-q8_0.gguf", {"gpus": "card"}, None),
    "canary": ("canary_asr", "transcription", "Canary-180M-Flash-GGUF/canary-180m-flash-f32.gguf", {}, None),
    "nemotron-asr": ("nemotron_asr", "transcription", "Nemotron-3.5-ASR-Streaming-0.6B-GGUF/nemotron-3.5-asr-streaming-0.6b-q8_0.gguf", {}, None),
    "nemotron-diar": ("nemotron_3_diar", "diarization", "Nemotron-3-Diarization-GGUF/nemotron-3-diarization-bf16.gguf", {}, None),
}
SPEAKER_MODEL = Path.home() / "ai/models/voice/speaker/wespeaker_en_voxceleb_resnet34_LM.onnx"


def words(text):
    """Lower-case words without punctuation."""
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def wer(said, heard):
    """Word error rate of what was heard against what was said."""
    truth, found = words(said), words(heard)
    row = list(range(len(found) + 1))
    for i, word in enumerate(truth, 1):
        diagonal, row[0] = row[0], i
        for j, other in enumerate(found, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (word != other))
    return round(row[-1] / max(1, len(truth)), 3)


def arguments():
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite-url", default="http://10.77.10.42:8442")
    parser.add_argument("--suite-key-file", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True, help="the audio place, such as ~/ai/models/audio")
    parser.add_argument("--skip", default="", help="comma-separated parts to leave out: music (ACE-Step), families, speech, browser")
    parser.add_argument("--only", default="", help="comma-separated PACKAGES names to qualify, besides parakeet")
    parser.add_argument("--card", default="CUDA1")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def natives():
    """Every audio child and native server: pid, parent, resident MiB and command."""
    rows = subprocess.run(["ps", "-eo", "pid,ppid,rss,args"], capture_output=True, text=True).stdout.splitlines()[1:]
    return [(int(r.split()[0]), int(r.split()[1]), int(r.split()[2]) // 1024, " ".join(r.split()[3:])[:90]) for r in rows
            if "audiocpp_server" in r or "soundroom/runtime.py" in r]


def gpu():
    """Used MiB on each card."""
    rows = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    return {int(line.split(",")[0]): int(line.split(",")[1]) for line in rows.splitlines() if "," in line}


async def outside(args):
    """Take the installed LoadCoach's lock, run the stack in a delegated service, and give the lock back."""
    import httpx
    key = args.suite_key_file.expanduser().read_text().strip()
    client = httpx.AsyncClient(base_url=args.suite_url, timeout=20, headers={"authorization": "Bearer " + key, "x-turnstile-no-wake": "1"})
    token = None

    async def ask(method, path, body=None):
        """Ask the installed suite, failing loudly."""
        answer = await client.request(method, path, json=body, headers={"X-LoadCoach-Lock": token} if token else {})
        if answer.status_code >= 400:
            raise RuntimeError(f"{path} answered HTTP {answer.status_code}")
        return answer.json()

    status = await ask("GET", "/loadcoach/status")
    if status.get("hostname") != socket.gethostname():
        raise SystemExit("The installed suite's compute host is not this host.")
    busy = {k: status.get(k) for k in ("running", "waiting", "others_running", "others_waiting", "busy", "lock", "draining", "batches") if status.get(k)}
    if busy:
        raise SystemExit("The installed suite is busy; nothing was taken: " + json.dumps(busy)[:300])
    args.output.mkdir(parents=True, exist_ok=False)
    token = (await ask("POST", "/loadcoach/lock", {"seconds": 90, "unload": True}))["token"]
    command = ["systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet", "-p", "Delegate=memory", "-p", "DelegateSubgroup=supervisor",
               "-p", "MemoryMax=24G", "-p", "MemorySwapMax=0", f"--working-directory={ROOT}", sys.executable, __file__, "--inside",
               "--output", str(args.output.resolve()), "--models", str(args.models.expanduser().resolve()), "--card", args.card,
               "--suite-key-file", str(args.suite_key_file), "--skip", args.skip, "--only", args.only]
    child = await asyncio.create_subprocess_exec(*command, start_new_session=True)
    reason = ""
    try:
        while child.returncode is None:
            try:
                await asyncio.wait_for(child.wait(), 10)
                break
            except TimeoutError:
                pass
            await ask("POST", "/loadcoach/lock", {"seconds": 90})
            status = await ask("GET", "/loadcoach/status")
            if status.get("waiting") or status.get("others_waiting"):
                reason = "foreground work was waiting, so the qualification stopped"
                os.killpg(child.pid, signal.SIGTERM)
                await asyncio.wait_for(child.wait(), 60)
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            await child.wait()
        await ask("POST", "/loadcoach/unlock")
        await client.aclose()
    print("Lock given back.", reason)
    return child.returncode or 0


def inside(args):
    """The stack and the scenario, inside the delegated service."""
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    import tomllib
    import httpx
    import uvicorn
    from front_helpers import Ports, Streamed, door, machine_key, own_copy
    from loadcoach.app import build_app as build_loadcoach
    from loadcoach.config import Config
    from loadcoach.groups import ABILITIES, OWN_KEY, Group
    from loadcoach.users import Users
    from turnstile.app import Settings as TurnstileSettings, build_app as build_turnstile
    from weightroom.app import Settings, build_app

    out = args.output
    (out / "data").mkdir()
    report = {"steps": [], "started": time.time(), "revision": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()}

    def step(name, **facts):
        """Keep one step's facts in the report and say it."""
        report["steps"].append({"step": name, "at": round(time.time() - report["started"], 1), **facts})
        print(name, json.dumps(facts)[:300], flush=True)

    groups = {"admin": Group(can=frozenset(ABILITIES) - OWN_KEY - {"superuser"}, max_sensitivity="restricted"),
              "plain": Group(can=frozenset({"load"})), "front": Group(can=frozenset({"sign-in", "mirror-users", "follow-settings"}))}
    users = Users(str(out / "data" / "accounts.db"), groups)
    for name, group in (("jordan", "admin"), ("pat", "plain")):
        users.add(name, group)
        users.change(name, password="correct horse")
    users.add("panel", "front")
    key_file = machine_key(out)
    engines = tomllib.load(open(ROOT / "settings/loadcoach.toml", "rb"))["engines"]

    class Later:
        """An app given its app afterwards."""
        app = None

        async def __call__(self, scope, receive, send):
            """Pass a request to the app given later."""
            await self.app(scope, receive, send)

    later = Later()
    compute = build_loadcoach(Config(database=str(out / "loadcoach.db"), data=str(out), gpus=("CUDA0", "CUDA1"), engines=engines,
                                     places={"audio": "/home/jpk/ai/models/audio"}, machine_key_file=key_file, front="http://front:18442"),
                              httpx.AsyncClient(timeout=60), door(later, 18442))
    turnstile = build_turnstile(TurnstileSettings(compute="compute", data=str(out / "data"), machine_key_file=key_file, ports=(18442, 18443, 18444)),
                                client=httpx.AsyncClient(transport=Ports({port: Streamed(compute) for port in (18442, 18443, 18444)}), timeout=None),
                                users=own_copy(users))
    later.app = turnstile
    weightroom = build_app(Settings(compute="http://compute", key=users.new_key("panel"), data=str(out / "data")), door(turnstile, 18442))

    async def stack(scope, receive, send):
        """WeightRoom on a real port, with compute's and turnstile's lifespans around it."""
        if scope["type"] == "lifespan":
            await receive()
            async with compute.router.lifespan_context(compute), turnstile.router.lifespan_context(turnstile), weightroom.router.lifespan_context(weightroom):
                await send({"type": "lifespan.startup.complete"})
                await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        await weightroom(scope, receive, send)

    async def until(check, seconds, every=1.0):
        """Wait for a condition, or fail."""
        began = time.monotonic()
        while time.monotonic() - began < seconds:
            if value := await check():
                return value
            await asyncio.sleep(every)
        raise TimeoutError("waited too long")

    skip = set(filter(None, args.skip.split(",")))
    root = args.models.expanduser()

    def profile(name):
        """An audio model's settings, from PACKAGES."""
        family, kind, relative, extra, _ = PACKAGES[name]
        path, extra = root / relative, dict(extra)
        on_card = extra.pop("gpus", None) == "card" or kind != "transcription"
        return {"engine": "audiocpp", "flags": {"model": str(path), "ctx-size": 0}, "gpus": [args.card] if on_card else 0,
                "alone": kind in ("music", "sound"), "audiocpp": {"family": family, "kind": kind, "path": str(path), "files": [],
                                                                  "host_ram_mib": 8192 if kind in ("music", "sound") else 4096,
                                                                  "limits": {"max_seconds": 240, "max_takes": 4}, **extra}}

    only = set(filter(None, args.only.split(","))) | {"parakeet"}
    present = [name for name, (_, _, relative, _, _) in PACKAGES.items() if (root / relative).exists() and (only == {"parakeet"} or name in only)]

    async def scenario(server):
        """Every SoundRoom page's work, as a browser asks for it."""
        while not server.started:
            await asyncio.sleep(.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=600, follow_redirects=True) as browser:
            step("signed in", status=(await browser.post("/sign-in", data={"name": "jordan", "password": "correct horse"})).status_code)
            for name in present:
                made = await browser.post("/soundroom-models", json={"name": name, "settings": profile(name), "new": True})
                step("profile " + name, status=made.status_code, error=None if made.status_code < 300 else made.text[:300])

            async def finished(identifier, kinds="take"):
                """The job, once it has ended."""
                async def done():
                    """The job if it has ended, else None."""
                    listed = (await browser.get("/soundroom-api/state?kinds=" + kinds)).json()
                    job = next((one for one in listed["jobs"] if one["id"] == identifier), None)
                    return job if job and job["status"] in ("completed", "failed", "cancelled", "expired") else None
                return await until(done, 900, 2)

            async def outputs(job, kinds="take"):
                """The job's retained outputs."""
                return [one for one in (await browser.get("/soundroom-api/state?kinds=" + kinds)).json()["assets"] if one["metadata"].get("job") == job["id"]]

            if "music" not in skip:
                before = gpu()
                started = await browser.post("/soundroom-api/jobs", json={"request": {"model": "ace-turbo", "prompt": "warm indie folk, acoustic guitar, soft male vocal",
                                                                                    "lyrics": "[verse]\nMorning light on the window\n[chorus]\nStay a while, stay a while",
                                                                                    "duration": 20, "n": 2, "seed": 7}})
                job = await finished(started.json()["id"])
                takes = await outputs(job)
                step("music", status=job["status"], takes=[(t["seconds"], t["metadata"].get("seed"), t["metadata"].get("model"), t["metadata"].get("metrics")) for t in takes],
                     gpu_before=before, gpu_after=gpu(), processes=natives())
                for take in takes:
                    got = await browser.get(f"/soundroom-api/assets/{take['id']}/content?format=mp3&download=1")
                    (out / f"{take['id']}.mp3").write_bytes(got.content)
                    step("download", status=got.status_code, bytes=len(got.content), type=got.headers.get("content-type"))
                repaint = await browser.post("/soundroom-api/jobs", json={"request": {"model": "ace-turbo", "prompt": "warm indie folk, acoustic guitar, soft male vocal",
                                                                                    "mode": "repaint", "repaint_start": 6, "repaint_end": 12, "n": 1, "seed": 9}, "source": takes[0]["id"]})
                job = await finished(repaint.json()["id"])
                step("repaint", status=job["status"], seconds=[one["seconds"] for one in await outputs(job)], source_seconds=takes[0]["seconds"])

                long = (await browser.post("/soundroom-api/jobs", json={"request": {"model": "ace-turbo", "prompt": "slow ambient drone", "duration": 200,
                                                                                   "n": 1, "seed": 3, "instrumental": True}})).json()["id"]

                async def generating():
                    """True once the long take is being generated, or has ended."""
                    listed = (await browser.get("/soundroom-api/state?kinds=take")).json()
                    job = next((one for one in listed["jobs"] if one["id"] == long), {})
                    return (job.get("progress") or {}).get("stage") == "generating" or job.get("status") not in ("in_progress", "validating")
                await until(generating, 300, .5)
                await asyncio.sleep(3)
                busy = gpu()
                await browser.post(f"/soundroom-api/jobs/{long}", json={"action": "cancel"})
                stopped = time.monotonic()
                job = await finished(long)

                async def freed():
                    """True once the card is empty and no native server is left."""
                    return gpu()[int(args.card.removeprefix("CUDA"))] < 500 and not [p for p in natives() if "audiocpp_server" in p[3]]
                await until(freed, 60, .25)
                step("stop", job=job["status"], gpu_while_generating=busy, freed_after_seconds=round(time.monotonic() - stopped, 1))

            speech = subprocess.run(["espeak-ng", "--stdout", "openWeightGym records music, sound and speech on this machine."], capture_output=True).stdout
            upload = (await browser.post("/soundroom-api/assets", json={"b64_audio": base64.b64encode(speech).decode(), "filename": "hello.wav", "kind": "recording"})).json()
            heard = await browser.post("/soundroom-api/transcribe", json={"asset": upload["id"], "model": "parakeet"}, timeout=300)
            step("transcribe", status=heard.status_code, text=heard.json().get("text"), segments=len(heard.json().get("segments", [])))

            if "qwen3-clone" in present and "speech" not in skip:
                voice = await browser.post("/soundroom-api/voices", json={"name": "Robot", "asset": upload["id"], "transcript": heard.json().get("text", ""), "consent": True})
                step("voice", status=voice.status_code)
                said = await browser.post("/soundroom-api/jobs", json={"request": {"model": "qwen3-clone", "seed": 5, "script": [
                    {"text": "Hello, this is the first line.", "voice_id": voice.json()["id"]},
                    {"text": "And this is the second line, after a short pause.", "voice_id": voice.json()["id"]}]}})
                job = await finished(said.json()["id"], "speech")
                spoken = await outputs(job, "speech")
                step("script", status=job["status"], error=job.get("error"), seconds=[one["seconds"] for one in spoken],
                     metrics=[one["metadata"].get("metrics") for one in spoken])
                if spoken:
                    back = await browser.post("/soundroom-api/transcribe", json={"asset": spoken[0]["id"], "model": "parakeet"}, timeout=300)
                    step("script heard back", text=back.json().get("text"))

            async def listen(asset_id):
                """What Parakeet hears in an output, as a check that it says something."""
                back = await browser.post("/soundroom-api/transcribe", json={"asset": asset_id, "model": "parakeet"}, timeout=300)
                return back.json().get("text") if back.status_code == 200 else f"HTTP {back.status_code}"

            if "families" not in skip:
                for name in present:
                    asked = PACKAGES[name][4]
                    if not asked:
                        continue
                    kinds = "take" if PACKAGES[name][1] == "music" else "sound"
                    began = time.monotonic()
                    started = await browser.post("/soundroom-api/jobs", json={"request": {"model": name, "n": 1, **asked}})
                    if started.status_code >= 300:
                        step("family " + name, status=started.status_code, error=started.text[:300])
                        continue
                    job = await finished(started.json()["id"], kinds)
                    made = await outputs(job, kinds)
                    lyrics_heard = await listen(made[0]["id"]) if made and asked.get("lyrics") else None
                    step("family " + name, status=job["status"], error=job.get("error"), seconds=round(time.monotonic() - began, 1),
                         outputs=[(one["seconds"], one["metadata"].get("metrics")) for one in made], lyrics_heard=lyrics_heard)

            narrator = None
            if "qwen3-design" in present and "speech" not in skip:
                sample = "Hello! This is how I sound when I read aloud for you."
                began = time.monotonic()
                designed = await browser.post("/soundroom-api/jobs", json={"request": {"model": "qwen3-design", "seed": 3, "script": [
                    {"text": sample, "instructions": "A warm, calm older male narrator with a low voice"}]}})
                job = await finished(designed.json()["id"], "speech")
                made = await outputs(job, "speech")
                step("design", status=job["status"], error=job.get("error"), seconds=round(time.monotonic() - began, 1),
                     outputs=[(one["seconds"], one["metadata"].get("metrics")) for one in made], heard=await listen(made[0]["id"]) if made else None)
                if made:
                    saved = await browser.post("/soundroom-api/voices", json={"name": "Designed narrator", "asset": made[0]["id"], "transcript": sample,
                                                                              "description": "warm older narrator", "designed": True})
                    step("designed voice saved", status=saved.status_code)
                    narrator = {"asset": made[0]["id"], "text": sample, "voice": saved.json()["id"]}

            if "sortformer" in present and "speech" not in skip:
                parts = [subprocess.run(["espeak-ng", "-v", voice, "--stdout", words], capture_output=True).stdout for voice, words in
                         (("en+m3", "Good morning. How was the trip?"), ("en+f4", "It was long, but the views were wonderful."),
                          ("en+m3", "I am glad you are back."))]
                import io, wave
                frames, rate = bytearray(), None
                for part in parts:
                    with wave.open(io.BytesIO(part)) as clip:
                        rate = clip.getframerate()
                        frames += clip.readframes(clip.getnframes()) + bytes(rate)  # half a second between speakers
                joined = io.BytesIO()
                with wave.open(joined, "wb") as writer:
                    writer.setnchannels(1), writer.setsampwidth(2), writer.setframerate(rate), writer.writeframes(bytes(frames))
                dialogue = (await browser.post("/soundroom-api/assets", json={"b64_audio": base64.b64encode(joined.getvalue()).decode(),
                                                                              "filename": "two-speakers.wav", "kind": "recording"})).json()
                for name in [one for one in present if PACKAGES[one][1] == "diarization"]:
                    turns = await browser.post("/soundroom-api/transcribe", json={"asset": dialogue["id"], "model": "parakeet", "diarization_model": name}, timeout=300)
                    found = turns.json()
                    step("diarization " + name, status=turns.status_code, speakers=sorted({str(one.get("speaker")) for one in found.get("segments", [])}),
                         segments=[(one.get("speaker"), one.get("text")) for one in found.get("segments", [])], turns=found.get("speaker_turns", [])[:8])

            if "qwen3-clone" in present and "speech" not in skip:
                from soundroom.voice import AudioSpeech
                key = users.new_key("jordan")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=turnstile), base_url="http://front:18442", timeout=300) as model_door:
                    async def request(method, path, body):
                        """Ask through turnstile as the voice service does, with an account's key."""
                        reply = await model_door.request(method, path, json=body, headers={"authorization": "Bearer " + key})
                        if reply.status_code >= 400:
                            raise RuntimeError(f"{path} HTTP {reply.status_code}: {reply.text[:200]}")
                        return reply.content
                    live = AudioSpeech({"recognition_model": "parakeet", "synthesis_model": "qwen3-clone", "trailing_silence_ms": 600}, request)
                    import io, wave
                    with wave.open(io.BytesIO(subprocess.run(["espeak-ng", "--stdout", "What is the weather like today?"], capture_output=True).stdout)) as clip:
                        pcm16 = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(clip.getframerate()), "-ac", "1",
                                                "-i", "pipe:0", "-ar", "16000", "-f", "s16le", "pipe:1"], input=clip.readframes(clip.getnframes()), capture_output=True).stdout
                    stream, final, began = live.stream(), None, time.monotonic()
                    for offset in range(0, len(pcm16) + 32000, 3200):
                        frame = (pcm16[offset:offset + 3200] or bytes(3200)).ljust(3200, b"\0")
                        _, final = await live.hear(stream, frame)
                        if final is not None:
                            break
                    heard_at = time.monotonic()
                    reference = await browser.get(f"/soundroom-api/assets/{upload['id']}/content")
                    spoken = await live.say("Hello, I am answering in your voice.", voice={"audio": base64.b64encode(reference.content).decode(), "text": heard.json().get("text", "")})
                    step("voice service speech", heard=final, recognition_seconds=round(heard_at - began, 2), speech_bytes=len(spoken),
                         speech_seconds=round(len(spoken) / 2 / 24000, 2), synthesis_seconds=round(time.monotonic() - heard_at, 2))

            if "speech" not in skip and narrator:
                import numpy
                import sherpa_onnx
                extractor = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(SPEAKER_MODEL), num_threads=2))

                def voiceprint(audio):
                    """A unit speaker embedding (WeSpeaker ResNet34) of a recording in any format ffmpeg reads."""
                    pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                                         input=audio, capture_output=True).stdout
                    stream = extractor.create_stream()
                    stream.accept_waveform(16000, numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768)
                    stream.input_finished()
                    vector = numpy.array(extractor.compute(stream))
                    return vector / numpy.linalg.norm(vector)

                async def content(asset_id):
                    """An asset's audio."""
                    return (await browser.get(f"/soundroom-api/assets/{asset_id}/content")).content
                reference, other = voiceprint(await content(narrator["asset"])), voiceprint(await content(upload["id"]))
                step("voiceprints", narrator_vs_espeak=round(float(reference @ other), 3))
                passage = "The quick brown fox jumps over the lazy dog, then rests beside the quiet river until the sun goes down."
                live_key = users.new_key("jordan")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=turnstile), base_url="http://front:18442", timeout=600,
                                             headers={"authorization": "Bearer " + live_key}) as model_door:
                    for name in present:
                        kind = PACKAGES[name][1]
                        if kind not in ("speech", "clone", "design"):
                            continue
                        line = {"text": passage} | ({"voice_id": narrator["voice"]} if kind == "clone" else {}) | (
                            {"instructions": "A bright young woman with a clear, friendly voice"} if kind == "design" else {})
                        timings, job = [], None
                        for _ in range(2):  # the first take loads the model; the second shows warm speed
                            began = time.monotonic()
                            started = await browser.post("/soundroom-api/jobs", json={"request": {"model": name, "seed": 4, "script": [line]}})
                            if started.status_code >= 300:
                                job = {"status": f"HTTP {started.status_code}", "error": started.text[:300]}
                                break
                            job = await finished(started.json()["id"], "speech")
                            timings.append(round(time.monotonic() - began, 1))
                            if job["status"] != "completed":
                                break
                        made = await outputs(job, "speech") if "id" in job else []
                        facts = {"status": job["status"], "error": job.get("error"), "cold_seconds": timings[:1], "warm_seconds": timings[1:]}
                        if made:
                            heard_back = await listen(made[-1]["id"])
                            seconds = made[-1]["seconds"]
                            facts |= {"audio_seconds": seconds, "warm_realtime_factor": round(timings[-1] / seconds, 2) if seconds else None,
                                      "heard": heard_back, "wer": wer(passage, heard_back), "metrics": made[-1]["metadata"].get("metrics")}
                            if kind == "clone":
                                printed = voiceprint(await content(made[-1]["id"]))
                                facts |= {"similarity": round(float(printed @ reference), 3), "similarity_to_other_voice": round(float(printed @ other), 3)}
                            asked = {"model": name, "input": "Sure, I can help with that.", "response_format": "pcm"}
                            if kind == "clone":
                                asked |= {"voice_audio": base64.b64encode(await content(narrator["asset"])).decode(), "voice_text": narrator["text"]}
                            if kind == "design":
                                asked["instructions"] = line["instructions"]
                            began = time.monotonic()
                            reply = await model_door.post("/v1/audio/speech", json=asked)
                            facts |= {"live_reply_seconds": round(time.monotonic() - began, 2), "live_reply_status": reply.status_code,
                                      "live_reply_audio_seconds": round(len(reply.content) / 2 / 24000, 2) if reply.status_code == 200 else None}
                        step("speech family " + name, **facts)

                truths = [("narrator", narrator["asset"], narrator["text"]), ("espeak", upload["id"], "openWeightGym records music, sound and speech on this machine.")]
                for name in [one for one in present if PACKAGES[one][1] == "transcription"]:
                    results = []
                    for label, asset_id, truth in truths:
                        began = time.monotonic()
                        back = await browser.post("/soundroom-api/transcribe", json={"asset": asset_id, "model": name}, timeout=600)
                        text = back.json().get("text") if back.status_code == 200 else f"HTTP {back.status_code}: {back.text[:200]}"
                        results.append({"input": label, "seconds": round(time.monotonic() - began, 1), "wer": wer(truth, text), "text": text})
                    step("transcription family " + name, results=results)

            if "browser" not in skip:
                from playwright.async_api import async_playwright
                async with async_playwright() as playwright:
                    chrome = await playwright.chromium.launch(channel="chrome", args=["--disable-gpu"])
                    context = await chrome.new_context(viewport={"width": 1400, "height": 1000})
                    await context.add_cookies([{"name": n, "value": v, "url": f"http://127.0.0.1:{PORT}"} for n, v in browser.cookies.items()])
                    tab = await context.new_page()
                    errors = []
                    tab.on("pageerror", lambda problem: errors.append(str(problem)))
                    found = {}
                    for path, ready in (("/chat/music", "#sr-list .sr-item"), ("/chat/speech", "#sr-model option"), ("/models/audio", "#am-catalogue .card"),
                                        ("/system/audio", "form"), ("/account/audio", "#ap-format")):
                        await tab.goto(f"http://127.0.0.1:{PORT}{path}")
                        await tab.wait_for_selector(ready, timeout=20000, state="attached")
                        await tab.wait_for_timeout(1500)
                        name = path.strip("/").replace("/", "-")
                        await tab.screenshot(path=str(out / f"{name}.png"), full_page=True)
                        await tab.set_viewport_size({"width": 390, "height": 900})
                        found[path] = await tab.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
                        await tab.screenshot(path=str(out / f"{name}-phone.png"))
                        await tab.set_viewport_size({"width": 1400, "height": 1000})
                    step("browser", errors=errors, scrolls_sideways_on_phone=found)
                    await chrome.close()

    async def main():
        """Serve the stack, run the scenario and write the report, whatever happens."""
        server = uvicorn.Server(uvicorn.Config(stack, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
        serving = asyncio.create_task(server.serve())
        try:
            await scenario(server)
            report["result"] = "completed"
        except Exception as problem:
            import traceback
            report["result"] = "failed: " + repr(problem)
            report["traceback"] = traceback.format_exc()
            print(report["traceback"], flush=True)
        finally:
            server.should_exit = True
            await serving
            report["gpu_end"], report["processes_end"] = gpu(), natives()
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    asyncio.run(main())
    return 0 if report["result"] == "completed" else 1


if __name__ == "__main__":
    options = arguments()
    sys.exit(inside(options) if options.inside else asyncio.run(outside(options)))
