"""Private audio child. Runner owns admission, cgroup and all descendants.

There is no independent job queue: one admitted request owns the engine. The native
server is started before the child reports ready; it uploads weights on the first request.
It is killed whenever a request does not finish, because audio.cpp cannot cancel a run.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from soundroom import model
from soundroom.requests import MAX_REQUEST_BYTES, checked_generation, checked_script, checked_speech, checked_transcription

NATIVE_ID = "audio"


def free_port():
    """Ask the OS for a private native listener port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def native_config(job, port):
    """One model, loaded at start, nothing the native server would evict or expose on its own."""
    settings = job["settings"]
    options = settings["audiocpp"]
    entry = {"id": NATIVE_ID, "family": options["family"], "path": options["path"], "task": model.TASKS[options["kind"]],
             "mode": "offline", "lazy": False}
    for key in ("load_options", "session_options"):
        if options.get(key):
            entry[key] = options[key]
    return {"host": "127.0.0.1", "port": port, "backend": "cuda" if job["cards"] else "cpu", "device": 0,
            "threads": options.get("threads", 4), "lazy_load": False, "max_loaded_models": 1, "idle_unload_ms": 0,
            "busy_timeout_ms": 0, "ui": False, "ui_management": False, "max_request_body_bytes": MAX_REQUEST_BYTES,
            "models": [entry]}


async def ffmpeg(arguments, data=b""):
    """Run ffmpeg on bytes; its log is returned for metrics and errors.

    Output named pipe:1 goes through a file: a WAV written to a pipe has no sizes in its header,
    which audio.cpp refuses.
    """
    with tempfile.TemporaryDirectory(prefix="suite-audio-ffmpeg-") as folder:
        target = str(Path(folder) / "out")
        arguments = [target if one == "pipe:1" else one for one in arguments]
        process = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-nostdin", "-y", *arguments,
                                                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, log = await process.communicate(data)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            raise ValueError("The audio could not be decoded: " + (log.decode(errors="replace").strip().splitlines() or ["ffmpeg failed"])[-1][:300])
        output = Path(target).read_bytes() if target in arguments else b""
    return output, log.decode(errors="replace")


async def to_wav(data, rate, channels):
    """Convert any ffmpeg-readable upload to the PCM WAV a family reads."""
    output, _ = await ffmpeg(["-i", "pipe:0", "-map_metadata", "-1", "-ac", str(channels), "-ar", str(rate),
                              "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"], data)
    return output


async def metrics(wav):
    """Measure what qualification checks: length, format, level, clipping and silence."""
    _, log = await ffmpeg(["-i", "pipe:0", "-af", "astats=measure_overall=Peak_level+RMS_level+Number_of_samples:measure_perchannel=none,"
                           "silencedetect=noise=-60dB:d=0.5,ebur128=peak=true", "-f", "null", "-"], wav)
    found = {}
    for key, pattern in (("rate", r"Stream #0:0.*?(\d+) Hz"), ("peak_dbfs", r"Peak level dB: (\S+)"),
                         ("rms_dbfs", r"RMS level dB: (\S+)"), ("loudness_lufs", r"Integrated loudness:\s*\n\s*I:\s*(\S+) LUFS"),
                         ("samples", r"Number of samples: (\d+)")):
        if match := re.search(pattern, log):
            found[key] = match[1]
    for key in ("rate", "samples"):
        if key in found:
            found[key] = int(found[key])
    if found.get("rate") and "samples" in found:
        found["duration"] = round(found.pop("samples") / found["rate"], 3)
    for key in ("peak_dbfs", "rms_dbfs", "loudness_lufs"):
        if key in found:
            found[key] = None if found[key] in ("-inf", "inf", "nan") else float(found[key])
    found["channels"] = 2 if re.search(r"Stream #0:0.*stereo", log) else 1 if re.search(r"Stream #0:0.*mono", log) else None
    found["silent_seconds"] = round(sum(float(value) for value in re.findall(r"silence_duration: (\S+)", log)), 3)
    return found


async def seamless(wav, length, fade):
    """Fold the tail over the head so the sound repeats without a seam: generate length + fade, keep length."""
    graph = (f"[0:a]asplit=3[h][m][t];[h]atrim=0:{fade},asetpts=N/SR/TB[head];[m]atrim={fade}:{length},asetpts=N/SR/TB[middle];"
             f"[t]atrim={length}:{length + fade},asetpts=N/SR/TB[tail];[tail][head]acrossfade=d={fade}:c1=tri:c2=tri[seam];[seam][middle]concat=n=2:v=0:a=1[out]")
    output, _ = await ffmpeg(["-i", "pipe:0", "-filter_complex", graph, "-map", "[out]", "-map_metadata", "-1", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"], wav)
    return output


def native_request(resolved, settings, seed, source_wav):
    """Translate openWeightGym's engine-neutral music contract to one family's native request."""
    options = settings["audiocpp"]
    family = model.CATALOG.get(options["family"], {})
    request = {"options": {key.removeprefix(options["family"] + "."): value for key, value in resolved["options"].items()}}
    caption = family.get("caption", "text")
    if caption == "text":
        request["text"] = resolved["prompt"]
    else:
        request["options"][caption] = resolved["prompt"]
    lyrics = family.get("instrumental", "") if resolved["instrumental"] else resolved["lyrics"]
    if lyrics and family.get("lyrics"):
        request["options"][family["lyrics"]] = lyrics
    if "duration" in resolved:
        seconds = resolved["duration"] + (fade_of(resolved["duration"]) if resolved.get("loop") else 0)
        if family.get("frames"):  # no duration option: both token bounds at the frame count make the length exact
            frames = round(seconds * family["frames"])
            request["options"] |= {"semantic_min_tokens": frames, "semantic_max_tokens": frames}
        else:
            request["options"]["duration_sec"] = seconds
    request["options"]["seed"] = seed
    mode = resolved["mode"]
    # Native field names are data in the family contract, not model-name branches.
    mode_value = family.get("mode_values", {}).get(mode)
    if family.get("mode_option") and mode_value is not None:
        request["options"][family["mode_option"]] = mode_value
    if mode == "repaint":
        for boundary in ("start", "end"):
            option = family.get("repaint_" + boundary + "_option")
            if option:
                value = resolved["repaint_" + boundary]
                request["options"][option] = str(value) if family.get("repaint_as_text") else value
    if source_wav is not None:
        request["audio_base64"] = base64.b64encode(source_wav).decode()
    return request


def fade_of(duration):
    """The crossfade a seamless loop of this length is given."""
    return round(min(1.0, duration / 4), 3)


async def gpu_peak(card, peak, every=.25):
    """Follow the memory used on the card a profile holds; per-process figures miss much of audio.cpp's."""
    while True:
        try:
            rows = await asyncio.create_subprocess_exec("nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", card.removeprefix("CUDA"),
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await rows.communicate()
            if output.strip().isdigit():
                peak["mib"] = max(peak.get("mib", 0), int(output))
        except OSError:
            return
        await asyncio.sleep(every)


SPEECH_MEDIA = {"wav": ("audio/wav", ["-c:a", "pcm_s16le", "-f", "wav"]), "mp3": ("audio/mpeg", ["-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3"]),
                "flac": ("audio/flac", ["-c:a", "flac", "-f", "flac"]), "opus": ("audio/ogg", ["-c:a", "libopus", "-b:a", "96k", "-f", "ogg"]),
                "pcm": ("audio/pcm", ["-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "s16le"])}


async def encoded(wav, form):
    """The WAV as the format a speech caller asked for."""
    media, arguments = SPEECH_MEDIA[form]
    if form == "wav":
        return media, wav
    output, _ = await ffmpeg(["-i", "pipe:0", "-map_metadata", "-1", *arguments, "pipe:1"], wav)
    return media, output


async def join(wavs, gap):
    """Join spoken lines into one recording with a short pause between them."""
    if len(wavs) == 1:
        return wavs[0]
    with tempfile.TemporaryDirectory(prefix="suite-audio-join-") as folder:
        inputs = []
        for index, wav in enumerate(wavs):
            path = Path(folder) / f"{index}.wav"
            path.write_bytes(wav)
            inputs += ["-i", str(path)]
        pause = f"aevalsrc=0:d={gap}" if gap else ""
        chains = "".join(f"[{index}:a]aformat=sample_fmts=s16:channel_layouts=mono,aresample=async=1[a{index}];" for index in range(len(wavs)))
        order = "".join(f"[a{index}]" + (f"[g{index}]" if gap and index < len(wavs) - 1 else "") for index in range(len(wavs)))
        gaps = "".join(f"{pause}[g{index}];" for index in range(len(wavs) - 1)) if gap else ""
        count = len(wavs) + (len(wavs) - 1 if gap else 0)
        graph = gaps + chains + order + f"concat=n={count}:v=0:a=1[out]"
        output, _ = await ffmpeg([*inputs, "-filter_complex", graph, "-map", "[out]", "-map_metadata", "-1", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"])
        return output


def speech_request(line, options, seed, reference, family=None):
    """Translate one spoken line to the native speech request."""
    label = (family or {}).get("speaker_label", "")
    text = line["text"] if not label or line["text"].startswith(label.split()[0]) else label + line["text"]
    request = {"model": NATIVE_ID, "input": text, "response_format": "json", "options": dict(options)}
    if seed >= 0:
        request["seed"] = str(seed)
    for key in ("voice", "instructions"):
        if line.get(key):
            request[key] = line[key]
    if reference is not None:
        request["voice_ref"] = {"type": "base64", "data": base64.b64encode(reference).decode()}
        if line.get("voice_text"):
            request["reference_text"] = line["voice_text"]
    return request


def segments(words, rate):
    """Group timed words into caption segments at sentence ends, pauses or every 15 seconds."""
    found, current = [], []
    for word in words:
        start, end = word["start_sample"] / rate, word["end_sample"] / rate
        if current and (start - current[-1]["end"] > .8 or end - current[0]["start"] > 15):
            found.append(current)
            current = []
        current.append({"word": word["word"], "start": round(start, 3), "end": round(end, 3)})
        if word["word"].rstrip().endswith((".", "?", "!")):
            found.append(current)
            current = []
    if current:
        found.append(current)
    return [{"id": index, "start": part[0]["start"], "end": part[-1]["end"], "text": " ".join(one["word"] for one in part)} for index, part in enumerate(found)]


def stamp(seconds, separator):
    """A subtitle timestamp, with the separator SRT or VTT uses."""
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(rest):02d}{separator}{int(round(rest % 1 * 1000)):03d}"


def transcript(value, form):
    """Shape a native detailed transcription as an OpenAI-compatible answer."""
    rate = value.get("sample_rate") or 16000
    words = [{"word": one["word"], "start": round(one["start_sample"] / rate, 3), "end": round(one["end_sample"] / rate, 3)} for one in value.get("words", [])]
    parts = segments(value.get("words", []), rate) if words else [{"id": 0, "start": 0, "end": 0, "text": value.get("text", "")}]
    if form == "text":
        return Response(value.get("text", ""), media_type="text/plain")
    if form in ("srt", "vtt"):
        separator = "," if form == "srt" else "."
        blocks = [(f"{part['id'] + 1}\n" if form == "srt" else "") + f"{stamp(part['start'], separator)} --> {stamp(part['end'], separator)}\n{part['text']}" for part in parts]
        return Response(("WEBVTT\n\n" if form == "vtt" else "") + "\n\n".join(blocks) + "\n", media_type="text/vtt" if form == "vtt" else "application/x-subrip")
    if form == "verbose_json":
        duration = (value.get("timing") or {}).get("audio_duration_ms")
        return JSONResponse({"task": "transcribe", "language": value.get("language", ""), "duration": round(duration / 1000, 3) if duration else None,
                             "text": value.get("text", ""), "words": words, "segments": parts})
    return JSONResponse({"text": value.get("text", "")})


class Native:
    """A subordinate native server whose full lifetime belongs to the admitted child."""

    def __init__(self, job):
        """Nothing runs until the first start."""
        self.job = job
        self.process = self.reader = self.client = None
        self.output = ""
        self.folder = None

    async def read_output(self):
        """Drain native output privately and retain only bounded ephemeral diagnostics."""
        while line := await self.process.stdout.readline():
            self.output = (self.output + line.decode(errors="replace"))[-262144:]

    async def start(self):
        """Start the native server and wait until its one model is loaded."""
        if self.process and self.process.returncode is None:
            return
        port = free_port()
        self.output = ""
        self.folder = tempfile.TemporaryDirectory(prefix="suite-audio-native-")
        config = Path(self.folder.name) / "server.json"
        config.write_text(json.dumps(native_config(self.job, port)))
        config.chmod(0o600)
        self.process = await asyncio.create_subprocess_exec(
            self.job["installation"]["executable"], "--config", str(config), "--no-ui", "--log",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=2**22,
            env={**os.environ, "TMPDIR": self.folder.name})  # it unpacks package configs there and never removes them
        self.reader = asyncio.create_task(self.read_output())
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=self.job["settings"]["audiocpp"].get("timeout", 1800))
        began = time.monotonic()
        while time.monotonic() - began < self.job["settings"]["audiocpp"].get("timeout", 1800):
            if self.process.returncode is not None:
                errors = [line for line in self.output.splitlines() if re.search(r"error|failed|cannot|invalid", line, re.I)]
                raise RuntimeError("audio.cpp failed to load: " + ("; ".join(errors[-3:])[-1200:] or "check the package files and memory"))
            try:
                answer = await self.client.get("/v1/models", timeout=5)
                if answer.status_code == 200 and any(one.get("loaded", True) for one in answer.json().get("data", [])):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(.2)
        raise TimeoutError("audio.cpp model loading timed out.")

    async def stop(self):
        """Stop and await native exit before a reservation is released."""
        if self.process and self.process.returncode is None:
            self.process.kill()  # it keeps nothing worth a clean exit, and SIGTERM mid-run makes it crash
            await self.process.wait()
        if self.reader:
            await self.reader
        if self.client:
            await self.client.aclose()
        if self.folder:
            self.folder.cleanup()
        self.process = self.reader = self.client = self.folder = None

    async def call(self, path, **sent):
        """One native call besides tasks/run; failures kill the server like a run's."""
        await self.start()
        done = False
        try:
            answer = await self.client.post(path, **sent)
            try:
                value = answer.json()
            except ValueError:
                value = {}
            if answer.status_code >= 400:
                message = (value.get("error") or {}).get("message") if isinstance(value.get("error"), dict) else value.get("error")
                raise RuntimeError("audio.cpp refused the request: " + str(message or answer.status_code)[:600])
            done = True
            return value
        finally:
            if not done:
                await self.stop()

    async def run(self, request):
        """Run one native task; a run that does not complete kills the server."""
        await self.start()
        done = False
        peak = {}
        watcher = asyncio.create_task(gpu_peak(self.job["cards"][0], peak)) if self.job["cards"] else None
        try:
            answer = await self.client.post("/v1/tasks/run", json={"model": NATIVE_ID, "request": request})
            try:
                value = answer.json()
            except ValueError:
                value = {}
            if answer.status_code >= 400:
                message = (value.get("error") or {}).get("message") if isinstance(value.get("error"), dict) else value.get("error")
                raise RuntimeError("audio.cpp refused the request: " + str(message or answer.status_code)[:600])
            done = True
            return {**value, "peak_vram_mib": peak.get("mib")}
        finally:
            if watcher:
                watcher.cancel()
            if not done:
                await self.stop()


def result_audio(value):
    """The WAV a native run returned, wherever this family puts it."""
    result = value.get("result", value)
    encoded = result.get("audio") or next((one.get("audio") for one in result.get("named_audio_outputs") or [] if one.get("audio")), None)
    if not encoded:
        raise RuntimeError("audio.cpp returned no audio.")
    return base64.b64decode(encoded)


async def script(job, asked, native):
    """Speak a script line by line in its voices, then join the lines into one recording."""
    settings = job["settings"]
    resolved = checked_script(asked, settings)
    family = model.CATALOG.get(settings["audiocpp"]["family"], {})
    start = time.monotonic()
    seed = resolved["seed"] if resolved["seed"] >= 0 else secrets.randbelow(2**32)
    yield {"type": "audio_generation.progress", "stage": "loading"}
    await native.start()
    spoken, references = [], {}
    for index, line in enumerate(resolved["lines"]):
        yield {"type": "audio_generation.progress", "stage": "speaking", "line": index, "lines": len(resolved["lines"])}
        reference = None
        if "voice_audio" in line:
            key = hashlib.sha256(line["voice_audio"]).hexdigest()
            if key not in references:
                references[key] = await to_wav(line["voice_audio"], 24000, 1)
            reference = references[key]
        value = await native.call("/v1/audio/speech", json=speech_request(line, resolved["options"], seed, reference, family))
        spoken.append(result_audio(value))
    yield {"type": "audio_generation.progress", "stage": "joining"}
    wav = await join(spoken, resolved["gap"])
    yield {"type": "audio_generation.completed", "take": 0, "b64_wav": base64.b64encode(wav).decode(), "seed": seed,
           "metrics": await metrics(wav), "native_seconds": round(time.monotonic() - start, 3), "elapsed_seconds": round(time.monotonic() - start, 3),
           "family": settings["audiocpp"]["family"], "kind": settings["audiocpp"]["kind"], "licence": family.get("licence", "See model card"), "commercial": family.get("commercial", "unknown"),
           "settings": {"lines": [{k: v for k, v in line.items() if k != "voice_audio"} for line in resolved["lines"]],
                        "seed": seed, "gap": resolved["gap"], "options": resolved["options"]}}


async def generation(job, asked, native):
    """Generate a bounded number of takes, reporting each as it finishes."""
    settings = job["settings"]
    resolved = checked_generation(asked, settings)
    model.check_options({**settings, "audiocpp": {**settings["audiocpp"], "defaults": resolved["options"]}}, job["installation"].get("manifest"))
    family = model.CATALOG.get(settings["audiocpp"]["family"], {})
    start = time.monotonic()
    seed = resolved["seed"] if resolved["seed"] >= 0 else secrets.randbelow(2**32)
    source = None
    if "audio" in resolved:
        yield {"type": "audio_generation.progress", "stage": "preparing"}
        source = await to_wav(resolved.pop("audio"), family.get("rate", 48000), family.get("channels", 2))
    yield {"type": "audio_generation.progress", "stage": "loading"}
    await native.start()
    for index in range(resolved["n"]):
        take_seed = (seed + index) % 2**32
        yield {"type": "audio_generation.progress", "stage": "generating", "take": index}
        began = time.monotonic()
        value = await native.run(native_request(resolved, settings, take_seed, source))
        wav = result_audio(value)
        if resolved["loop"]:
            wav = await seamless(wav, resolved["duration"], fade_of(resolved["duration"]))
        measured = await metrics(wav) | {"peak_vram_mib": value.get("peak_vram_mib")}
        yield {"type": "audio_generation.completed", "take": index, "b64_wav": base64.b64encode(wav).decode(),
               "seed": take_seed, "metrics": measured, "native_seconds": round(time.monotonic() - began, 3),
               "family": settings["audiocpp"]["family"], "kind": settings["audiocpp"]["kind"],
               "licence": family.get("licence", "See model card"), "commercial": family.get("commercial", "unknown"),
               "elapsed_seconds": round(time.monotonic() - start, 3),
               "settings": {key: value for key, value in resolved.items() if key != "audio"}}


def create_app(job, native=None):
    """Expose only authenticated private health and audio routes to the owning Runner."""
    native = native or Native(job)
    busy = False

    @asynccontextmanager
    async def lifespan(app):
        """Start the native server with the child, and stop it with the child."""
        await native.start()  # a package that cannot load fails here, before LoadCoach calls it ready
        try:
            yield
        finally:
            await native.stop()

    def authorized(request):
        """True only for LoadCoach's private token."""
        return secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + job["token"])

    async def health(request):
        """Ready once the native server answers, for LoadCoach only."""
        if not authorized(request):
            return JSONResponse({"error": {"message": "Unauthorized"}}, 401)
        return JSONResponse({"status": "ready"})

    kind = job["settings"]["audiocpp"]["kind"]
    spoken = kind in ("speech", "clone", "design")

    async def body_of(request):
        """Read a bounded JSON request body."""
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                raise ValueError("Audio request exceeds 96 MiB.")
        return json.loads(body)

    async def once(request, checked, work):
        """Run one whole-file request; a failure part-way kills the native server before the slot frees."""
        nonlocal busy
        if not authorized(request):
            return JSONResponse({"error": {"message": "Unauthorized"}}, 401)
        if busy:
            return JSONResponse({"error": {"message": "This audio runtime already has an admitted job."}}, 409)
        try:
            resolved = checked(await body_of(request), job["settings"])
        except (ValueError, TypeError) as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        busy = True
        completed = False
        try:
            answer = await work(resolved)
            completed = True
            return answer
        except ValueError as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        except Exception as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 502)
        finally:
            with anyio.CancelScope(shield=True):
                if not completed:
                    await native.stop()
                busy = False

    async def speak(resolved):
        """Speak one line and encode it as the caller asked."""
        line = resolved["line"]
        reference = await to_wav(line["voice_audio"], 24000, 1) if "voice_audio" in line else None
        family = model.CATALOG.get(job["settings"]["audiocpp"]["family"], {})
        value = await native.call("/v1/audio/speech", json=speech_request(line, resolved["options"], resolved["seed"], reference, family))
        media, content = await encoded(result_audio(value), resolved["format"])
        return Response(content, media_type=media, headers={"cache-control": "no-store"})

    async def transcribe(resolved):
        """Transcribe, or find who spoke when, in one recording."""
        wav = await to_wav(resolved["audio"], 16000, 1)
        if kind == "diarization":
            value = await native.run({"audio_base64": base64.b64encode(wav).decode()})
            result = value.get("result", value)
            rate = result.get("sample_rate") or 16000
            turns = [{"speaker": turn.get("speaker_id", turn.get("speaker")), "start": round(turn.get("start_sample", 0) / rate, 3),
                      "end": round(turn.get("end_sample", 0) / rate, 3)} for turn in result.get("speaker_turns", [])]
            return JSONResponse({"speaker_turns": turns, "duration": (await metrics(wav)).get("duration")})
        form = {"model": NATIVE_ID, **({"language": resolved["language"]} if resolved["language"] else {})}
        value = await native.call("/v1/audio/transcriptions/details", data=form, files={"file": ("audio.wav", wav, "audio/wav")})
        return transcript(value, resolved["format"])

    async def speech(request: Request):
        """OpenAI-shaped speech, for speech, clone and design profiles."""
        if not spoken:
            return JSONResponse({"error": {"message": "This model does not speak."}}, 400)
        return await once(request, checked_speech, speak)

    async def transcriptions(request: Request):
        """Transcription or diarization, for listening profiles."""
        if kind not in ("transcription", "diarization"):
            return JSONResponse({"error": {"message": "This model does not transcribe."}}, 400)
        return await once(request, checked_transcription, transcribe)

    async def generations(request: Request):
        """Music, sound or spoken-script takes, streamed or whole."""
        nonlocal busy
        if not authorized(request):
            return JSONResponse({"error": {"message": "Unauthorized"}}, 401)
        if busy:
            return JSONResponse({"error": {"message": "This audio runtime already has an admitted job."}}, 409)
        if kind in ("transcription", "diarization", "alignment"):
            return JSONResponse({"error": {"message": "This model listens; it does not generate audio."}}, 400)
        try:
            asked = await body_of(request)
            (checked_script if spoken else checked_generation)(asked, job["settings"])
        except (ValueError, TypeError) as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        busy = True

        async def events():
            """The job's events; a job that does not finish kills the native server."""
            nonlocal busy
            completed = False
            sequence = (script if spoken else generation)(job, asked, native)
            try:
                async for event in sequence:
                    yield event
                completed = True
            finally:
                with anyio.CancelScope(shield=True):
                    await sequence.aclose()
                    if not completed:
                        await native.stop()
                    busy = False

        if asked.get("stream"):
            async def stream():
                """The events as server-sent events, ending with an error event on failure."""
                sequence = events()
                try:
                    async for event in sequence:
                        yield "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                except Exception as problem:
                    yield "event: error\ndata: " + json.dumps({"error": {"message": str(problem)}}) + "\n\n"
                finally:
                    await sequence.aclose()
            return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-store", "x-accel-buffering": "no"})
        try:
            output = []
            async for event in events():
                if event["type"] == "audio_generation.completed":
                    output.append({key: value for key, value in event.items() if key != "type"})
            return JSONResponse({"created": int(time.time()), "data": output}, headers={"cache-control": "no-store"})
        except ValueError as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 400)
        except Exception as problem:
            return JSONResponse({"error": {"message": str(problem)}}, 502)

    return Starlette(lifespan=lifespan, routes=[Route("/health", health), Route("/v1/audio/generations", generations, methods=["POST"]),
                                                Route("/v1/audio/speech", speech, methods=["POST"]),
                                                Route("/v1/audio/transcriptions", transcriptions, methods=["POST"]),
                                                Route("/v1/audio/diarizations", transcriptions, methods=["POST"])])


if __name__ == "__main__":
    import uvicorn
    job = json.loads(Path(sys.argv[1]).read_text())
    uvicorn.run(create_app(job), host="127.0.0.1", port=job["port"], access_log=False, log_level="warning", timeout_graceful_shutdown=10)
