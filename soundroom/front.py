"""Front audio API and assets, run through the existing batch executor; no inference scheduler."""
import asyncio
import base64
import json
import math
import re
import secrets
import time

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from loadcoach import observability as observation
from soundroom import stream
from soundroom.requests import MAX_REQUEST_BYTES

GENERATION = "/v1/audio/generations"
FORMATS = {"wav": ("audio/wav", ["-c:a", "pcm_s16le", "-f", "wav"]), "flac": ("audio/flac", ["-c:a", "flac", "-f", "flac"]),
           "mp3": ("audio/mpeg", ["-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3"]), "opus": ("audio/ogg", ["-c:a", "libopus", "-b:a", "160k", "-f", "ogg"])}
OUTPUT_KIND = {"music": "take", "sound": "sound", "speech": "speech", "clone": "speech", "design": "speech"}
LYRICS_PROMPT = ("You write music generation prompts. From the idea, return only a JSON object with two keys: "
                 "\"caption\": comma-separated genre, mood, instruments, tempo and vocal style, at most 300 characters; "
                 "\"lyrics\": song lyrics with section tags such as [verse], [chorus] and [bridge] on their own lines, "
                 "or an empty string for an instrumental. No commentary.")


def error(status, message):
    """Return a consistent error without exposing other owners."""
    return JSONResponse({"error": {"message": message}}, status)


def audio_bytes(content, media, headers, requested_range=None, head=False):
    """Serve an authorized recording with byte ranges so browsers can seek and loop."""
    headers = {**headers, 'accept-ranges': 'bytes'}
    length, status, start, end = len(content), 200, 0, len(content) - 1
    if requested_range and requested_range.startswith('bytes=') and ',' not in requested_range:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested_range)
        try:
            if not match or not any(match.groups()):
                raise ValueError()
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), length - 1) if last else length - 1
            else:
                start = max(0, length - int(last))
            if start > end or start >= length:
                raise ValueError()
        except ValueError:
            return Response(status_code=416, headers={**headers, 'content-range': f'bytes */{length}'})
        status = 206
        headers['content-range'] = f'bytes {start}-{end}/{length}'
    headers['content-length'] = str(max(0, end - start + 1))
    return Response(b'' if head else content[start:end + 1], status_code=status, media_type=media, headers=headers)


async def transcode(content, format):
    """Encode stored audio for download; tags and prompts are never written into the file."""
    from soundroom.runtime import ffmpeg
    media, arguments = FORMATS[format]
    output, _ = await ffmpeg(["-loglevel", "error", "-i", "pipe:0", "-map_metadata", "-1", "-vn", *arguments, "pipe:1"], content)
    return media, output


def lyrics_of(text):
    """Read the model's JSON answer, tolerating text around the object."""
    match = re.search(r"\{.*\}", text or "", re.S)
    value = json.loads(match[0]) if match else {}
    caption, lyrics = value.get("caption", ""), value.get("lyrics", "")
    if not isinstance(caption, str) or not isinstance(lyrics, str) or not caption.strip():
        raise ValueError("The model did not return a caption and lyrics; try again or another model.")
    return {"caption": caption.strip()[:1000], "lyrics": lyrics.strip()[:20000]}


def outcome(batches, batch):
    """A job's status and why it failed: a job whose every take failed has failed, whatever the batch says."""
    if batch is None:
        return "completed", None
    counts, status = batch.get("request_counts", {}), batch["status"]
    if not counts.get("failed"):
        return status, None
    first = (batches.content(batch["error_file_id"]) or b"").split(b"\n", 1)[0] if batch.get("error_file_id") else b""
    try:
        value = json.loads(first) if first.strip() else {}
        problem = value.get("error") or (value.get("response", {}).get("body", {}).get("error")) or {}
        error = problem.get("message") if isinstance(problem, dict) else str(problem)
    except (ValueError, AttributeError, TypeError):
        error = None
    error = error or "A take failed; no detailed error was retained."
    return ("failed" if status == "completed" and counts["failed"] == counts.get("total") else status), error


class Front:
    """SoundRoom's front-owned assets and voices, and ordinary batch execution hooks."""

    def __init__(self, store, batches, users, log=None):
        """Initialize private audio state without loading model weights."""
        self.store, self.batches, self.users, self.log = store, batches, users, log
        self.progress, self.transient = {}, {}

    def visible(self, user, batch):
        """Additional stable-identity check for audio-owned batch rows."""
        owner = self.store.owner(batch["id"])
        return owner is None or owner == user.identity

    async def catalogue(self, user):
        """The authenticated combined catalogue's audio models; forbidden models stay unknown."""
        answer = (await self.batches.door.get("/v1/models", headers={"authorization": "Bearer " + self.batches.internal_key(user.name), "x-turnstile-no-wake": "1"})
                  if self.batches.door else await self.batches.ask_compute(user, "GET", "/v1/models"))
        if answer is None or answer.status_code != 200:
            raise ValueError("The audio model inventory is unavailable.")
        return [one for one in answer.json().get("data", []) if one.get("capabilities", {}).get("audio")]

    def job_owner(self, identifier):
        """Look up durable or temporary ownership without trusting a request header."""
        return self.transient.get(identifier, {}).get("owner") or self.store.owner(identifier)

    def retain_event(self, user, event, kind, job=None, line=None, quiet=False, model=None):
        """Keep finished takes on front; progress is held only as the newest event."""
        if job and self.job_owner(job) != user.identity:
            raise ValueError("There is no such audio job of yours.")
        if job and event.get("type") == "audio_generation.progress":
            self.progress[job] = {**event, "at": time.time()}
        if job and "error" in event:
            self.progress[job] = {**event, "stage": "failed", "at": time.time()}
        if event.get("type") != "audio_generation.completed":
            return event
        content = base64.b64decode(event["b64_wav"], validate=True)
        metadata = {key: value for key, value in event.items() if key not in ("b64_wav", "type")}
        if model:
            metadata["model"] = model
        if job:
            metadata.update(job=job, batch_line=line)
        asset = self.store.save(user.identity, content, kind=OUTPUT_KIND.get(kind, "take"), filename=f"{kind or 'audio'}-{event.get('seed', 0)}.wav",
                                metadata=metadata, temporary=quiet, job=job if job and not quiet else None)
        if job:
            self.progress[job] = {"type": "audio_generation.progress", "stage": "completed", "at": time.time()}
        return {**event, "asset_id": asset["id"], "asset_url": "/soundroom/assets/" + asset["id"] + "/content"}

    async def model_of(self, user, name):
        """The caller's permitted audio model by name, or None."""
        return next((one for one in await self.catalogue(user) if one["id"] == name), None)

    async def capture(self, request, user, response, asked):
        """Retain completed takes of an API call on front while passing its events on."""
        if response.status_code >= 400:
            return response
        quiet = request.headers.get("x-loadcoach-log") == "off"
        job, line = request.headers.get("x-soundroom-job"), request.headers.get("x-soundroom-line")
        if job and self.job_owner(job) != user.identity:
            return error(404, "There is no such audio job of yours.")
        if quiet:
            observation.silence()
        selected = await self.model_of(user, asked.get("model"))
        kind = (selected or {}).get("capabilities", {}).get("audio", "music")
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type", "content-encoding")}
        headers.update({"cache-control": "no-store", "x-suite-audio-owner": "front"})

        async def chunks():
            """The upstream body as bytes, streamed or whole."""
            if hasattr(response, "body_iterator"):
                async for chunk in response.body_iterator:
                    yield chunk.encode() if isinstance(chunk, str) else chunk
            else:
                yield response.body
        if "text/event-stream" in response.headers.get("content-type", ""):
            async def forwarded():
                """Keep finished takes on front while passing every event on."""
                try:
                    async for event in events(chunks()):
                        yield stream.encode(self.retain_event(user, event, kind, job, line, quiet, asked.get("model")))
                except Exception as problem:
                    yield stream.encode({"type": "error", "error": {"message": str(problem)}})
                finally:
                    iterator = getattr(response, "body_iterator", None)
                    if iterator and hasattr(iterator, "aclose"):
                        await iterator.aclose()
            return StreamingResponse(forwarded(), media_type="text/event-stream", headers=headers, background=response.background)
        body = bytearray()
        async for chunk in chunks():
            body.extend(chunk)
            if len(body) > stream.MAX_EVENT:
                return error(502, "The audio response exceeds the response limit.")
        try:
            decoded = json.loads(body)
            for item in decoded.get("data", []):
                event = self.retain_event(user, {"type": "audio_generation.completed", **item}, kind, job, line, quiet, asked.get("model"))
                item.update({k: v for k, v in event.items() if k != "type"})
            return JSONResponse(decoded, headers=headers, background=response.background)
        except (ValueError, KeyError, TypeError) as problem:
            return error(502, "Could not retain the generated audio: " + str(problem))

    async def execute_line(self, number, line, body, user, headers, *, quiet=False):
        """Run one take of a batch through the same authenticated audio API."""
        if not quiet and self.store.owner(number) is None:
            self.store.bind_job(number, user.identity, "", body["model"], body)
        if self.job_owner(number) != user.identity:
            return httpx.Response(403, json={"error": {"message": "Audio job owner changed."}})
        done = [asset for asset in self.store.listing(user.identity, job=number) if str(asset["metadata"].get("batch_line")) == str(line["line"])]
        if done:  # a line that already produced its take is not generated again after a restart
            return httpx.Response(200, json={"data": [{"asset_id": asset["id"], **asset["metadata"]} for asset in done]})
        sent = {key: value for key, value in body.items() if key != "source_asset_id"}
        if source := body.get("source_asset_id"):
            content = self.store.content(user.identity, source)
            if content is None:
                return httpx.Response(400, json={"error": {"message": "The source audio was deleted or is unavailable."}})
            sent["audio"] = base64.b64encode(content).decode()
        if "script" in sent:
            try:
                sent["script"] = [self.spoken_line(user, line) for line in sent["script"]]
            except ValueError as problem:
                return httpx.Response(400, json={"error": {"message": str(problem)}})
        sent["stream"] = True
        headers = {**headers, "x-soundroom-job": number, "x-soundroom-line": str(line["line"])}
        if quiet:
            headers = {k: v for k, v in headers.items() if k not in ("x-loadcoach-trace", "x-loadcoach-priority")}
            headers["x-loadcoach-log"] = "off"
        result, request_id = [], ""
        async with self.batches.door.stream("POST", GENERATION, json=sent, headers=headers, timeout=None) as response:
            request_id = response.headers.get("x-loadcoach-request-id", "")
            if response.status_code >= 400:
                await response.aread()
                return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
            async for event in events(response.aiter_bytes()):
                if "error" in event:
                    return httpx.Response(502, json={"error": event["error"]})
                if event.get("type") == "audio_generation.completed":
                    result.append({k: v for k, v in event.items() if k not in ("type", "b64_wav")})
        if not result:
            return httpx.Response(502, json={"error": {"message": "The audio stream ended without a finished take."}})
        return httpx.Response(200, json={"created": int(time.time()), "data": result}, headers={"x-loadcoach-request-id": request_id})

    def spoken_line(self, user, line):
        """A script line with its saved voice replaced by the voice's reference and transcript."""
        line = dict(line)
        if identifier := line.pop("voice_id", None):
            found = self.store.voice_reference(user.identity, identifier, user.group_name)
            if not found:
                raise ValueError("A voice in the script was deleted or is no longer shared with you.")
            content, words = found
            line["voice_audio"] = base64.b64encode(content).decode()
            if words:
                line["voice_text"] = words
        return line

    async def state(self, request, user):
        """Permitted audio models, private and shared assets, voices, jobs and preferences."""
        try:
            before = request.query_params.get("before")
            before = float(before) if before is not None else None
            before_id = request.query_params.get("before_id")
            if before is not None and not math.isfinite(before):
                raise ValueError("Invalid audio library cursor.")
            if before_id and (before is None or not re.fullmatch(r"audio_[a-f0-9]{32}", before_id)):
                raise ValueError("Invalid audio library cursor.")
        except (ValueError, TypeError):
            return error(400, "Invalid audio library cursor.")
        try:
            models = await self.catalogue(user)
        except ValueError:
            models = []
        jobs = []
        for row in self.store.jobs(user.identity):
            batch = self.batches.store.get(row["id"])
            status, problem_message = outcome(self.batches.store, batch)
            progress = self.progress.get(row["id"])
            if progress and progress.get("stage") == "failed" and status == "completed":
                status = "failed" if not (batch or {}).get("request_counts", {}).get("completed") else status
                problem_message = problem_message or (progress.get("error") or {}).get("message")
            jobs.append({**row, "status": status, "error": problem_message, "request_counts": batch.get("request_counts", {}) if batch else {},
                         "progress": progress})
        for identifier, row in self.transient.items():
            if row["owner"] == user.identity:
                jobs.insert(0, {"id": identifier, **{k: v for k, v in row.items() if k not in ("owner", "task")}, "progress": self.progress.get(identifier)})
        kinds = request.query_params.get("kinds", "take,sound,speech,reference,recording").split(",")
        assets = self.store.listing(user.identity, user.group_name, kinds=[k for k in kinds if k], limit=101,
                                    before=before, before_id=before_id, tag=request.query_params.get("tag") or None,
                                    favourites=request.query_params.get("favourites") == "1")
        more, assets = len(assets) > 100, assets[:100]
        for asset in assets:
            metadata = asset["metadata"]
            payload = self.store.job(user.identity, metadata.get("job")) if asset["mine"] else None
            if asset["temporary"] and asset["mine"]:
                payload = self.transient.get(metadata.get("job"), {}).get("settings")
            asset["reuse"] = {"request": {**metadata.get("settings", {}), **(payload or {}).get("request", {}),
                                          "model": metadata.get("model"), "seed": metadata.get("seed", -1)},
                              "source": (payload or {}).get("source")}
            asset["reuse"]["request"]["options"] = {**metadata.get("settings", {}).get("options", {}),
                                                     **(payload or {}).get("request", {}).get("options", {})}
        next_page = {"before": assets[-1]["created"], "before_id": assets[-1]["id"]} if more else None
        return JSONResponse({"models": models, "assets": assets, "next": next_page,
                             "voices": self.store.voices(user.identity, user.group_name), "jobs": jobs,
                             "groups": sorted(self.users.groups), "preferences": self.store.preferences(user.identity),
                             "can_manage": user.can("manage-models"), "can_unlogged": user.can("unlogged")}, headers={"cache-control": "no-store"})

    async def upload(self, request, user):
        """Keep a bounded source or reference upload and its retention choice."""
        try:
            asked = await self.read_json(request)
            temporary = asked.get("temporary", False)
            if type(temporary) is not bool:
                raise ValueError("temporary must be true or false.")
            if temporary and not user.can("unlogged"):
                return error(403, "Your account may not keep temporary no-log audio.")
            if temporary:
                observation.silence()
            kind = asked.get("kind", "reference")
            if kind not in ("reference", "recording"):
                raise ValueError("Uploads are references or recordings.")
            content = base64.b64decode(asked.get("b64_audio", ""), validate=True)
            asset = await asyncio.to_thread(self.store.save, user.identity, content, kind=kind, filename=asked.get("filename", "upload.wav"), temporary=temporary)
            return JSONResponse(asset, 201, headers={"cache-control": "no-store"})
        except (ValueError, OSError) as problem:
            return error(400, str(problem))

    async def asset(self, request, user):
        """Authorize playback, downloads, changes, sharing and deletion on every request."""
        identifier = request.path_params["identifier"]
        record = self.store.asset(user.identity, identifier, user.group_name)
        if not record:
            return error(404, "There is no such audio of yours.")
        if record.get("temporary"):
            observation.silence()
        try:
            if request.method == "DELETE":
                if not self.store.delete(user.identity, identifier):
                    return error(403, "Only the owner may delete this audio.")
                return JSONResponse({"deleted": True, "id": identifier})
            if request.method == "POST":
                asked = await self.read_json(request)
                if "groups" in asked:
                    shared = self.store.share(user.identity, identifier, asked.pop("groups"), set(self.users.groups))
                    if not asked:
                        return JSONResponse({"id": identifier, "shared_with": shared})
                return JSONResponse(self.store.update(user.identity, identifier, asked))
        except ValueError as problem:
            return error(409 if request.method == "DELETE" else 400, str(problem))
        content = self.store.content(user.identity, identifier, user.group_name)
        if content is None:
            return error(404, "This audio file is unavailable.")
        format = request.query_params.get("format", "")
        headers = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
        if record.get("temporary"):
            headers["x-suite-audio-temporary"] = "1"
        media = "audio/wav"
        if format:
            if format not in FORMATS:
                return error(400, "Download formats are " + ", ".join(FORMATS) + ".")
            try:
                media, content = await transcode(content, format)
            except ValueError as problem:
                return error(502, str(problem))
        if request.query_params.get("download"):
            extension = {"opus": "ogg"}.get(format, format or "wav")
            headers["content-disposition"] = 'attachment; filename="' + re.sub(r"[^\w.-]", "_", record["filename"].rsplit(".", 1)[0])[:80] + "." + extension + '"'
        return audio_bytes(content, media, headers, request.headers.get('range'), request.method == 'HEAD')

    @staticmethod
    async def read_json(request):
        """Read a bounded JSON object without buffering an unlimited upload."""
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                raise ValueError("Audio request exceeds 96 MiB.")
        value = json.loads(body or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Request must be a JSON object.")
        return value

    def lines(self, asked):
        """Expand a request for N takes into sequential batch lines, one take each, so other work can run between them.

        A script is one line: it becomes one recording.
        """
        body = dict(asked.get("request", {}))
        if "script" in body:
            if not isinstance(body["script"], list) or not body["script"] or len(body["script"]) > 500:
                raise ValueError("A script has 1–500 lines.")
            return [{"custom_id": "script", "body": {**body, "stream": True}}]
        count = body.pop("n", 1)
        if type(count) is not int or not 1 <= count <= 16:
            raise ValueError("Ask for 1–16 takes.")
        seed = body.get("seed", -1)
        if type(seed) is not int:
            raise ValueError("seed must be a whole number.")
        if source := asked.get("source"):
            body["source_asset_id"] = source
        return [{"custom_id": f"take-{index + 1}", "body": {**body, "n": 1, "stream": True, "seed": (seed + index) % 2**32 if seed >= 0 else -1}}
                for index in range(count)]

    async def start(self, request, user):
        """Submit an audio job to the established front batch executor."""
        try:
            asked = await self.read_json(request)
            lines = self.lines(asked)
            selected = await self.model_of(user, lines[0]["body"].get("model"))
            if not selected or selected.get("capabilities", {}).get("audio") not in OUTPUT_KIND:
                raise ValueError("Choose an available music, sound or speech model you may use.")
            profile = selected.get("audio_profile", {})
            body = lines[0]["body"]
            lyrics = body.get("lyrics", "")
            if profile.get("lyrics_required") and (body.get("instrumental") or not isinstance(lyrics, str) or not lyrics.strip()):
                raise ValueError("This model needs lyrics; choose ACE-Step for an instrumental.")
            for line in lines[0]["body"].get("script", []):
                if isinstance(line, dict) and line.get("voice_id") and not self.store.voice(user.identity, line["voice_id"], user.group_name):
                    raise ValueError("There is no such voice you may use.")
            sources = [asked["source"]] if asked.get("source") else []
            for identifier in sources:
                if not self.store.asset(user.identity, identifier):
                    raise ValueError("There is no such source audio of yours.")
            temporary = asked.get("temporary", False)
            if type(temporary) is not bool:
                raise ValueError("temporary must be true or false.")
            if any(self.store.asset(user.identity, key).get("temporary") for key in sources) and not temporary:
                raise ValueError("Temporary source audio requires a temporary no-log job.")
            if temporary:
                if not user.can("unlogged"):
                    return error(403, "Your account may not make no-log requests.")
                observation.silence()
                identifier = "quiet_audio_" + secrets.token_hex(12)
                self.transient[identifier] = {"owner": user.identity, "status": "in_progress", "created": time.time(),
                                              "model": selected["id"], "settings": asked}

                async def execute():
                    """Run a temporary job's takes without keeping job history."""
                    try:
                        for index, line in enumerate(lines):
                            current = self.users.by_identity(user.identity)
                            if not current:
                                raise ValueError("The audio job owner is no longer enabled.")
                            answer = await self.execute_line(identifier, {"line": index}, line["body"], current,
                                                             {"authorization": "Bearer " + self.batches.internal_key(current.name)}, quiet=True)
                            if answer.status_code >= 400:
                                raise ValueError(answer.json().get("error", {}).get("message", "Generation failed."))
                        self.transient[identifier]["status"] = "completed"
                    except asyncio.CancelledError:
                        self.transient[identifier]["status"] = "cancelled"
                        raise
                    except Exception as problem:
                        self.transient[identifier].update(status="failed", error=str(problem))
                task = asyncio.create_task(execute())
                self.transient[identifier]["task"] = task
                self.batches.carrying.add(task)
                task.add_done_callback(self.batches.carrying.discard)
                return JSONResponse({"id": identifier, "temporary": True}, 201)
            self.batches.store.models = __import__("turnstile.batches", fromlist=["Pins"]).Pins(await self.batches.model_ids(user))
            source = self.batches.store.put_file(user.name, "audio.jsonl", "batch", b"")
            batch = self.batches.store.start(user.name, GENERATION, source["id"], lines, {"soundroom": True, "owner_id": user.identity})
            self.store.bind_job(batch["id"], user.identity, selected.get("model_id", ""), selected["id"], asked, sources)
            self.batches.work(batch["id"], self.users.by_name)
            return JSONResponse({"id": batch["id"], "temporary": False}, 201)
        except (ValueError, KeyError, TypeError) as problem:
            return error(400, str(problem))

    async def control(self, request, user):
        """Stop only the caller's own audio job."""
        number = request.path_params["identifier"]
        if self.job_owner(number) != user.identity:
            return error(404, "There is no such audio job of yours.")
        if number in self.transient:
            observation.silence()
            self.transient[number]["task"].cancel()
            return JSONResponse({"id": number, "status": "cancelling"})
        batch = self.batches.store.get(number)
        if not batch:
            return error(404, "There is no such audio job of yours.")
        self.batches.store.cancelling(number)
        if (task := self.batches.working.get(number)) and not task.done():
            task.cancel()
        else:
            self.batches.store.finish(number)
        return JSONResponse({"id": number, "status": self.batches.store.get(number)["status"]})

    async def voices(self, request, user):
        """Save or delete a named voice; a cloned voice needs the speaker's permission, which is put on record."""
        try:
            if request.method == "DELETE":
                if not self.store.delete_voice(user.identity, request.path_params["identifier"]):
                    return error(404, "There is no such voice of yours.")
                return JSONResponse({"deleted": True})
            asked = await self.read_json(request)
            if asked.keys() - {"name", "asset", "transcript", "description", "consent", "designed"}:
                raise ValueError("A voice has name, asset, transcript, description, consent and designed.")
            designed = asked.get("designed", False) is True
            if not designed and asked.get("consent") is not True:
                raise ValueError("Saving a cloned voice needs your confirmation that you have the speaker's permission.")
            voice = self.store.save_voice(user.identity, asked.get("name"), asked.get("asset"), asked.get("transcript", ""),
                                          asked.get("description", ""), None if designed else time.time())
            if self.log:
                self.log.add("soundroom", "voice-saved", user.name, request.client.host if request.client else "",
                             voice=voice["id"], designed=designed, consent=not designed)
            return JSONResponse(voice, 201)
        except ValueError as problem:
            return error(400, str(problem))

    async def voice_list(self, request, user):
        """The caller's voices and those shared with the caller's group, for the voice gateway's chooser."""
        return JSONResponse({"voices": [{key: voice[key] for key in ("id", "name", "mine", "description")} for voice in self.store.voices(user.identity, user.group_name)]},
                            headers={"cache-control": "no-store"})

    async def voice_reference(self, request, user):
        """A visible voice's reference recording and words, for a spoken conversation in that voice."""
        found = self.store.voice_reference(user.identity, request.path_params["identifier"], user.group_name)
        if not found:
            return error(404, "There is no such voice you may use.")
        content, words = found
        return JSONResponse({"audio": base64.b64encode(content).decode(), "text": words}, headers={"cache-control": "no-store"})

    async def share_voice(self, request, user):
        """Replace the groups one of the caller's voices is shared with."""
        try:
            asked = await self.read_json(request)
            return JSONResponse({"shared_with": self.store.share(user.identity, request.path_params["identifier"], asked.get("groups"), set(self.users.groups))})
        except ValueError as problem:
            return error(400, str(problem))

    async def preferences(self, request, user):
        """Read or update the caller's audio preferences."""
        try:
            return JSONResponse(self.store.preferences(user.identity, await self.read_json(request) if request.method == "POST" else None))
        except ValueError as problem:
            return error(400, str(problem))

    async def transcribe(self, request, user):
        """Transcribe one of the caller's recordings, optionally with speaker turns, and keep the result with it."""
        try:
            asked = await self.read_json(request)
            if asked.keys() - {"asset", "model", "diarization_model", "language"}:
                raise ValueError("Transcribe takes asset, model, diarization_model and language.")
            record = self.store.asset(user.identity, asked.get("asset"), user.group_name)
            if not record:
                return error(404, "There is no such audio of yours.")
            content = self.store.content(user.identity, record["id"], user.group_name)
            audio = base64.b64encode(content).decode()
            key = {"authorization": "Bearer " + self.batches.internal_key(user.name)}
            sent = {"model": asked.get("model"), "file": audio, "response_format": "verbose_json"}
            if asked.get("language"):
                sent["language"] = asked["language"]
            answer = await self.batches.door.post("/v1/audio/transcriptions", json=sent, headers=key, timeout=None)
            if answer.status_code >= 400:
                return Response(answer.content, answer.status_code, media_type="application/json")
            result = answer.json()
            if asked.get("diarization_model"):
                turns = await self.batches.door.post("/v1/audio/diarizations", json={"model": asked["diarization_model"], "file": audio}, headers=key, timeout=None)
                if turns.status_code >= 400:
                    return Response(turns.content, turns.status_code, media_type="application/json")
                result["speaker_turns"] = turns.json().get("speaker_turns", [])
                for segment in result.get("segments", []):
                    segment["speaker"] = speaker_at(result["speaker_turns"], (segment["start"] + segment["end"]) / 2)
            result.update(model=asked.get("model"), diarization_model=asked.get("diarization_model"), at=time.time())
            if record["mine"] and not record["temporary"]:
                self.store.add_transcript(user.identity, record["id"], result)
            return JSONResponse(result, headers={"cache-control": "no-store"})
        except ValueError as problem:
            return error(400, str(problem))

    async def lyrics(self, request, user):
        """Draft a style caption and tagged lyrics from an idea with a chat model the caller may use."""
        try:
            asked = await self.read_json(request)
            idea, model = asked.get("idea", ""), asked.get("model") or "chat"
            if not isinstance(idea, str) or not 1 <= len(idea.strip()) <= 4000 or not isinstance(model, str):
                raise ValueError("Describe the song idea in up to 4000 characters.")
            seconds = asked.get("duration", 30)
            instrumental = asked.get("instrumental", False)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 3600 or type(instrumental) is not bool:
                raise ValueError("Choose a duration of 1–3600 seconds and an instrumental or vocal draft.")
            instruction = LYRICS_PROMPT + f" The requested clip is {seconds:g} seconds; keep the structure and lyrics short enough for that duration."
            if instrumental:
                instruction += " This is instrumental: lyrics must be empty, and the caption must specify no vocals."
            else:
                instruction += " Write singable lyrics with short lines; use only as many sections as the clip can fit."
            body = {"model": model, "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": idea.strip()}],
                    "temperature": 0.8, "max_tokens": 1500}
            answer = await self.batches.door.post("/v1/chat/completions", json=body, timeout=300,
                                                  headers={"authorization": "Bearer " + self.batches.internal_key(user.name)})
            if answer.status_code >= 400:
                return Response(answer.content, answer.status_code, media_type="application/json")
            return JSONResponse(lyrics_of(answer.json()["choices"][0]["message"].get("content")))
        except (ValueError, KeyError, IndexError, TypeError) as problem:
            return error(400, str(problem))

    def routes(self, caller, log):
        """Expose front-owned audio operations through the existing API-key authority."""
        from turnstile.accounts import signed_in
        self.log = self.log or log

        def route(path, handler, method="GET"):
            """One SoundRoom handler behind the API-key authority."""
            return Route(path, signed_in(caller, log, handler), methods=[method])
        return [route("/soundroom/state", self.state), route("/soundroom/assets", self.upload, "POST"),
                route("/soundroom/assets/{identifier}", self.asset, "DELETE"), route("/soundroom/assets/{identifier}", self.asset, "POST"),
                route("/soundroom/assets/{identifier}/content", self.asset),
                route("/soundroom/jobs", self.start, "POST"), route("/soundroom/jobs/{identifier}", self.control, "POST"),
                route("/soundroom/voices", self.voice_list), route("/soundroom/voices/{identifier}/reference", self.voice_reference),
                route("/soundroom/voices", self.voices, "POST"), route("/soundroom/voices/{identifier}", self.voices, "DELETE"),
                route("/soundroom/voices/{identifier}/share", self.share_voice, "POST"),
                route("/soundroom/preferences", self.preferences), route("/soundroom/preferences", self.preferences, "POST"),
                route("/soundroom/lyrics", self.lyrics, "POST"), route("/soundroom/transcribe", self.transcribe, "POST")]


def speaker_at(turns, moment):
    """The speaker whose turn covers a moment, as the diarization model names them."""
    for turn in turns:
        if turn.get("start", 0) <= moment <= turn.get("end", 0):
            return turn.get("speaker")
    return None


async def events(chunks):
    """Decode complete SSE data events across arbitrary chunk boundaries."""
    pending = bytearray()
    async for chunk in chunks:
        pending.extend(chunk.replace(b"\r", b""))
        if len(pending) > stream.MAX_EVENT:
            raise ValueError("Audio stream event exceeds its size limit.")
        while b"\n\n" in pending:
            frame, _, rest = pending.partition(b"\n\n")
            pending = bytearray(rest)
            lines = [line[5:].strip() for line in frame.split(b"\n") if line.startswith(b"data:")]
            if not lines or lines == [b"[DONE]"]:
                continue
            decoded = json.loads(b"\n".join(lines))
            if not isinstance(decoded, dict):
                raise ValueError("Invalid audio stream event.")
            yield decoded
    if pending.strip():
        raise ValueError("Audio stream ended during an event.")
