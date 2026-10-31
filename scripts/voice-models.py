"""Download checksum-pinned CPU bundles, or reproduce the recorded gateway prompts.

Run with the compute virtualenv: python scripts/voice-models.py /path/to/models/voice
Add --prompts settings/examples/voice.toml to also generate the committed PCM and telephone prompts.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request


def install(root):
    """Check an entire download before extracting, then verify every installed file."""
    manifest = json.loads((Path(__file__).resolve().parents[1] / "voice/models.json").read_text())
    root.mkdir(parents=True, exist_ok=True)
    for bundle in manifest["bundles"]:
        folder = root / bundle["name"]
        if not folder.exists():
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                archive = Path(temporary) / "bundle.tar.bz2"
                urllib.request.urlretrieve(bundle["url"], archive)
                if hashlib.sha256(archive.read_bytes()).hexdigest() != bundle["archive_sha256"]:
                    raise ValueError("Download checksum differs: " + bundle["name"])
                with tarfile.open(archive) as source:
                    source.extractall(temporary, filter="data")
                (Path(temporary) / bundle["name"]).rename(folder)
        for relative, expected in bundle["files"].items():
            if hashlib.sha256((folder / relative).read_bytes()).hexdigest() != expected:
                raise ValueError("Installed checksum differs: " + relative)
        print("Verified", bundle["name"])


async def prompts(config):
    """Rebuild fallback assets with the effective default profile's speaker, not the warmup voice."""
    from loadcoach.settings import with_changes
    from voice import prompt_recordings
    from voice.speech import Speech
    from voice.telephone import Conversion
    settings, _ = with_changes("voice", config)
    speech = Speech(settings)
    folder = Path(__file__).resolve().parents[1] / "voice/prompts"
    try:
        speaker, revision = prompt_recordings.specification(settings)
        bundle = await prompt_recordings.generate(speech, speaker, revision)
        for name, pcm in prompt_recordings.decode(bundle).items():
            (folder / (name + ".pcm")).write_bytes(pcm)
            (folder / (name + ".ulaw")).write_bytes(Conversion(cancel_echo=False).send(pcm))
        (folder / "recording.json").write_text(json.dumps(dict(speaker=speaker, revision=revision, sample_rate=24000), indent=2) + "\n")
        print("Recorded greetings with speaker", speaker)
    finally:
        speech.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--prompts", type=Path)
    args = parser.parse_args()
    install(args.folder)
    if args.prompts:
        asyncio.run(prompts(args.prompts))
