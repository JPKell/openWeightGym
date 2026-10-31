"""Strict audio request normalization; no server-local paths or native arguments."""
import base64
import binascii
import math

from soundroom import model

MAX_REQUEST_BYTES = 96 * 1024 * 1024
GENERATION_FIELDS = {"model", "prompt", "lyrics", "instrumental", "duration", "seed", "n", "mode", "audio",
                     "repaint_start", "repaint_end", "options", "loop", "stream", "user"}


def number(value, label, low, high):
    """Accept a finite JSON number within bounds."""
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a number from {low} to {high}.")
    return value


def encoded_audio(value, most_mib):
    """Decode bounded base64 audio; its format is checked when it is converted."""
    if not isinstance(value, str) or not value:
        raise ValueError("audio must be base64-encoded audio.")
    if value.startswith("data:"):
        value = value.partition(",")[2]
    if len(value) > (most_mib * 1024 * 1024 * 4) // 3 + 4:
        raise ValueError(f"audio exceeds {most_mib} MiB.")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("audio must be valid base64.") from None


def checked_generation(asked, settings):
    """Normalize one music or sound request against the admitted profile's family and limits."""
    if not isinstance(asked, dict):
        raise ValueError("The request must be an object.")
    if unknown := asked.keys() - GENERATION_FIELDS:
        raise ValueError("Unknown audio generation field(s): " + ", ".join(sorted(unknown)))
    options = settings["audiocpp"]
    family = model.CATALOG.get(options["family"], {})
    most = model.limits(settings)
    defaults = options.get("defaults", {})
    resolved = {"prompt": asked.get("prompt", ""), "lyrics": asked.get("lyrics", ""), "instrumental": asked.get("instrumental", False),
                "mode": asked.get("mode", "song"), "n": asked.get("n", 1), "seed": asked.get("seed", -1),
                "options": {**defaults, **asked.get("options", {})}}
    if not isinstance(resolved["prompt"], str) or not isinstance(resolved["lyrics"], str):
        raise ValueError("prompt and lyrics must be text.")
    if len(resolved["prompt"]) + len(resolved["lyrics"]) > most["max_text_chars"]:
        raise ValueError(f"prompt and lyrics exceed {most['max_text_chars']} characters.")
    if not resolved["prompt"].strip():
        raise ValueError("Describe the music or sound in prompt.")
    if resolved["lyrics"] and "lyrics" not in family:
        raise ValueError(f"{options['family']} does not take lyrics.")
    if type(resolved["instrumental"]) is not bool:
        raise ValueError("instrumental must be true or false.")
    if resolved["instrumental"] and resolved["lyrics"]:
        raise ValueError("An instrumental request has no lyrics.")
    if family.get("lyrics_required") and (resolved["instrumental"] or not resolved["lyrics"].strip()):
        raise ValueError(f"{options['family']} needs lyrics; choose ACE-Step for an instrumental.")
    if resolved["mode"] not in family.get("modes", ["song"]):
        raise ValueError(f"{options['family']} supports modes: {', '.join(family.get('modes', ['song']))}.")
    if type(resolved["n"]) is not int or not 1 <= resolved["n"] <= most["max_takes"]:
        raise ValueError(f"n must be 1–{most['max_takes']} takes.")
    if type(resolved["seed"]) is not int or not -1 <= resolved["seed"] < 2**32:
        raise ValueError("seed must be -1 (random) or 0 to 4294967295.")
    if "duration" in asked:
        resolved["duration"] = number(asked["duration"], "duration", 1, most["max_seconds"])
    model.scalars(resolved["options"], "request option")
    if resolved["mode"] == "song":
        if "audio" in asked or "repaint_start" in asked or "repaint_end" in asked:
            raise ValueError("Source audio and a repaint window belong to continue, cover or repaint modes.")
    else:
        if "audio" not in asked:
            raise ValueError(f"Mode {resolved['mode']} needs source audio.")
        resolved["audio"] = encoded_audio(asked["audio"], most["max_audio_mib"])
    if resolved["mode"] == "repaint":
        start = number(asked.get("repaint_start"), "repaint_start", 0, most["max_seconds"])
        end = number(asked.get("repaint_end"), "repaint_end", 0, most["max_seconds"])
        if end <= start:
            raise ValueError("repaint_end must be after repaint_start.")
        resolved |= {"repaint_start": start, "repaint_end": end}
    elif "repaint_start" in asked or "repaint_end" in asked:
        raise ValueError("A repaint window needs mode repaint.")
    resolved["loop"] = asked.get("loop", False)
    if type(resolved["loop"]) is not bool or resolved["loop"] and (options["kind"] != "sound" or resolved["mode"] != "song" or "duration" not in resolved):
        raise ValueError("A seamless loop is for a new sound of a stated duration.")
    if type(asked.get("stream", False)) is not bool:
        raise ValueError("stream must be true or false.")
    return resolved


SPEECH_FIELDS = {"model", "input", "voice", "voice_audio", "voice_text", "instructions", "response_format", "speed", "seed", "options", "stream_format", "user"}
SCRIPT_FIELDS = {"model", "script", "seed", "gap", "options", "stream", "user"}
LINE_FIELDS = {"text", "voice", "voice_audio", "voice_text", "instructions"}
TRANSCRIPTION_FIELDS = {"model", "file", "language", "prompt", "response_format", "timestamp_granularities", "temperature", "user"}
SPEECH_FORMATS = ("wav", "mp3", "flac", "opus", "pcm")
TRANSCRIPT_FORMATS = ("json", "text", "srt", "vtt", "verbose_json")


def checked_line(line, settings, most):
    """One spoken line: its text, and a preset voice, a reference recording or a designed-voice description."""
    if not isinstance(line, dict) or line.keys() - LINE_FIELDS:
        raise ValueError("A script line has text, voice, voice_audio, voice_text and instructions.")
    text = line.get("text", "")
    if not isinstance(text, str) or not text.strip() or len(text) > most["max_text_chars"]:
        raise ValueError(f"Each line needs 1–{most['max_text_chars']} characters of text.")
    kind = settings["audiocpp"]["kind"]
    resolved = {"text": text.strip()}
    for key in ("voice", "voice_text", "instructions"):
        if key in line:
            if not isinstance(line[key], str) or len(line[key]) > 4000:
                raise ValueError(f"{key} must be text of up to 4000 characters.")
            resolved[key] = line[key]
    if "voice_audio" in line:
        if kind != "clone":
            raise ValueError("Only a cloning profile takes a reference recording.")
        resolved["voice_audio"] = encoded_audio(line["voice_audio"], 8)
    if kind == "clone" and "voice_audio" not in resolved:
        raise ValueError("A cloning profile needs a reference recording (voice_audio).")
    if kind == "design" and not resolved.get("instructions", "").strip():
        raise ValueError("A voice-design profile needs a description of the voice (instructions).")
    return resolved


def checked_speech(asked, settings):
    """Normalize an OpenAI-shaped speech request against the admitted profile."""
    if not isinstance(asked, dict):
        raise ValueError("The request must be an object.")
    if unknown := asked.keys() - SPEECH_FIELDS:
        raise ValueError("Unknown speech field(s): " + ", ".join(sorted(unknown)))
    line = checked_line({key: asked[key] for key in LINE_FIELDS - {"text"} if key in asked} | {"text": asked.get("input", "")},
                        settings, model.limits(settings))
    form = asked.get("response_format", "mp3")
    if form not in SPEECH_FORMATS:
        raise ValueError("response_format is one of " + ", ".join(SPEECH_FORMATS) + ".")
    if "stream_format" in asked:
        raise ValueError("Streamed speech belongs to spoken chat; ask for a whole file here.")
    seed = asked.get("seed", -1)
    if type(seed) is not int or not -1 <= seed < 2**32:
        raise ValueError("seed must be -1 (random) or 0 to 4294967295.")
    options = {**settings["audiocpp"].get("defaults", {}), **asked.get("options", {})}
    model.scalars(options, "request option")
    if "speed" in asked:
        options["speed"] = number(asked["speed"], "speed", .25, 4)
    return {"line": line, "format": form, "seed": seed, "options": options}


def checked_script(asked, settings):
    """Normalize a Studio script: spoken lines in order, joined into one recording."""
    if not isinstance(asked, dict):
        raise ValueError("The request must be an object.")
    if unknown := asked.keys() - SCRIPT_FIELDS:
        raise ValueError("Unknown script field(s): " + ", ".join(sorted(unknown)))
    most = model.limits(settings)
    script = asked.get("script")
    if not isinstance(script, list) or not 1 <= len(script) <= 500:
        raise ValueError("A script has 1–500 lines.")
    lines = [checked_line(line, settings, most) for line in script]
    if sum(len(line["text"]) for line in lines) > most["max_text_chars"]:
        raise ValueError(f"The script exceeds {most['max_text_chars']} characters.")
    seed = asked.get("seed", -1)
    if type(seed) is not int or not -1 <= seed < 2**32:
        raise ValueError("seed must be -1 (random) or 0 to 4294967295.")
    gap = number(asked.get("gap", .3), "gap", 0, 5)
    options = {**settings["audiocpp"].get("defaults", {}), **asked.get("options", {})}
    model.scalars(options, "request option")
    if type(asked.get("stream", False)) is not bool:
        raise ValueError("stream must be true or false.")
    return {"lines": lines, "seed": seed, "gap": gap, "options": options}


def checked_transcription(asked, settings):
    """Normalize a transcription or diarization request whose audio arrives base64-encoded."""
    if not isinstance(asked, dict):
        raise ValueError("The request must be an object.")
    if unknown := asked.keys() - TRANSCRIPTION_FIELDS:
        raise ValueError("Unknown transcription field(s): " + ", ".join(sorted(unknown)))
    form = asked.get("response_format", "json")
    if form not in TRANSCRIPT_FORMATS:
        raise ValueError("response_format is one of " + ", ".join(TRANSCRIPT_FORMATS) + ".")
    language = asked.get("language", "")
    if not isinstance(language, str) or len(language) > 16 or not isinstance(asked.get("prompt", ""), str):
        raise ValueError("language is a short code and prompt is text.")
    return {"audio": encoded_audio(asked.get("file"), model.limits(settings)["max_audio_mib"]), "format": form, "language": language}


def request_body(content_type, body):
    """Accept JSON or the OpenAI SDK's multipart transcription form, whose file becomes base64."""
    import email
    import email.policy
    import json
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Audio request exceeds 96 MiB.")
    if not content_type.lower().startswith("multipart/form-data"):
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Audio request must be an object.")
        return value
    message = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body, policy=email.policy.HTTP)
    if not message.is_multipart():
        raise ValueError("Invalid multipart audio form.")
    value = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        content = part.get_payload(decode=True) or b""
        if name == "file":
            value["file"] = base64.b64encode(content).decode()
        elif name in ("timestamp_granularities[]", "timestamp_granularities"):
            value.setdefault("timestamp_granularities", []).append(content.decode())
        elif not name or name in value:
            raise ValueError("Repeated or missing audio form field.")
        else:
            value[name] = float(content.decode()) if name == "temperature" else content.decode()
    if "file" not in value:
        raise ValueError("Send the audio as the form's file field.")
    return value
