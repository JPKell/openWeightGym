"""Compare the specified CPU speech bundles using repeatable English microphone/phone audio.

Writes generated test audio and JSON measurements to the ignored voice-test folder.
This measures synthetic speech, not human accents, room noise or speakerphone echo.
"""

import asyncio
import audioop
import copy
import json
from pathlib import Path
import time
import tomllib
import wave

from voice.speech import Speech
from voice.telephone import Conversion

TEXTS = ["What is the capital of France?", "Please tell me a short story about a friendly robot.",
         "LoadCoach runs on the compute machine. The front server is called K server."]


async def main():
    """Run the speech check voice verification command."""
    base = tomllib.loads(Path("settings/defaults/voice.toml").read_text()) | tomllib.loads(Path("settings/examples/voice.toml").read_text())
    for key in ("encoder", "decoder", "joiner", "tokens"):
        base["recognition"][key] = base["recognition"][key].replace("en-2023-06-26", "en-20M-2023-02-17").replace("avg-1-chunk-16-left-128", "avg-1")
    for key in ("model", "voices", "tokens", "data_dir", "lexicon"):
        base["synthesis"][key] = base["synthesis"][key].replace("kokoro-multi-lang-v1_0", "kokoro-int8-multi-lang-v1_1").replace("model.onnx", "model.int8.onnx")
    base["synthesis"]["speaker"] = 0
    output = Path("voice-test")
    output.mkdir(exist_ok=True)
    report = []
    for synthesis in ("v1_1", "v1_0"):
        for recognition in ("light", "balanced"):
            settings = copy.deepcopy(base)
            if synthesis == "v1_0":
                for key in ("model", "voices", "tokens", "data_dir", "lexicon"):
                    settings["synthesis"][key] = settings["synthesis"][key].replace("kokoro-int8-multi-lang-v1_1", "kokoro-multi-lang-v1_0").replace("model.int8.onnx", "model.onnx")
                settings["synthesis"]["speaker"] = 3 # af_heart
            if recognition == "balanced":
                for key in ("encoder", "decoder", "joiner", "tokens"):
                    settings["recognition"][key] = settings["recognition"][key].replace("en-20M-2023-02-17", "en-2023-06-26").replace("avg-1.int8", "avg-1-chunk-16-left-128.int8")
            speech = Speech(settings)
            started = time.monotonic()
            await speech.warm()
            warm = time.monotonic() - started
            for number, text in enumerate(TEXTS):
                started = time.monotonic()
                pcm = await speech.say(text)
                synthesis_time = time.monotonic() - started
                with wave.open(str(output / f"{synthesis}-{number}.wav"), "wb") as file:
                    file.setnchannels(1)
                    file.setsampwidth(2)
                    file.setframerate(24000)
                    file.writeframes(pcm)
                for source in ("microphone", "phone"):
                    if source == "phone":
                        converter = Conversion(cancel_echo=False)
                        audio = converter.receive(converter.send(pcm))
                    else:
                        audio, _ = audioop.ratecv(pcm, 2, 1, 24000, 16000, None)
                    # Live callers have already sent silence before speaking.
                    audio = bytes(16000) + audio + bytes(32000)
                    stream, finals = speech.stream(), []
                    started = time.monotonic()
                    for i in range(0, len(audio), 640):
                        partial, final = await speech.hear(stream, audio[i:i + 640])
                        if final:
                            finals.append(final)
                    _, final = await speech.hear(stream, b"", True)
                    if final:
                        finals.append(final)
                    row = dict(synthesis=synthesis, recognition=recognition, source=source, text=text,
                               heard=" ".join(finals), audio_seconds=len(pcm)/48000,
                               recognition_seconds=time.monotonic()-started, synthesis_seconds=synthesis_time,
                               warm_seconds=warm)
                    report.append(row)
                    print(json.dumps(row), flush=True)
            speech.close()
    (output / "speech-comparison.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
