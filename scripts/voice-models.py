"""Download the CPU speech bundles, or reproduce the recorded gateway prompts.

Each download is checked against the checksums pinned in voice/models.json. Upstream rebuilds
these release archives from time to time, so a download that no longer matches its pin is not
an error by itself: it is installed when its files are still the pinned ones, or when it matches
the checksum its publisher lists. When the publisher lists none, it is installed unverified, with
a warning. Only a download that differs from the publisher's own checksum (a damaged one) stops.

The checksums of what was installed are kept beside each bundle, and a bundle whose files no
longer match them is downloaded again.

Run with the compute virtualenv: python scripts/voice-models.py /path/to/models/voice
Add --prompts settings/examples/voice.toml to also generate the committed PCM and telephone prompts.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tempfile
import urllib.parse
import urllib.request


def sha256(path):
    """Hash a file in pieces, so a large model is never held in memory whole."""
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for piece in iter(lambda: file.read(1 << 20), b""):
            digest.update(piece)
    return digest.hexdigest()


def file_sums(folder):
    """Every file in a bundle folder, with its checksum."""
    return {str(path.relative_to(folder)): sha256(path) for path in sorted(folder.rglob("*")) if path.is_file()}


def mismatched(folder, files):
    """The expected files that are changed or missing in an installed bundle."""
    return [name for name, expected in files.items() if not (folder / name).is_file() or sha256(folder / name) != expected]


def published_sha256(url):
    """The SHA-256 GitHub lists for a release download, or None when it lists none or cannot be asked."""
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)", url)
    if not match:
        return None
    owner, repository, tag, name = (urllib.parse.unquote(part) for part in match.groups())
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repository}/releases/tags/{urllib.parse.quote(tag)}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "openWeightGym-installer"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, ValueError):
        return None
    assets = release.get("assets", []) if isinstance(release, dict) else []
    digest = next((str(asset.get("digest") or "") for asset in assets if asset.get("name") == name), "")
    return digest.removeprefix("sha256:") if digest.startswith("sha256:") else None


def download(bundle, temporary):
    """Fetch a bundle's archive; say whether it matched its pin, its publisher's checksum, or neither could tell."""
    archive = Path(temporary) / "bundle.tar.bz2"
    for _ in range(2):
        urllib.request.urlretrieve(bundle["url"], archive)
        digest = sha256(archive)
        if digest == bundle["archive_sha256"]:
            return archive, "pinned"
        published = published_sha256(bundle["url"])
        if published is None:
            return archive, "unconfirmed"
        if published == digest:
            return archive, "published"
        print(f"{bundle['name']}: the download differs from the checksum its publisher lists; downloading it again")
    raise ValueError(f"The {bundle['name']} speech bundle from {bundle['url']} differed from the checksum its "
                     "publisher lists twice, so the download is damaged and was not installed.")


def fetch(bundle, root):
    """Download, check and unpack one bundle in place of any copy already there; return its file checksums."""
    folder = root / bundle["name"]
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        archive, confirmed = download(bundle, temporary)
        with tarfile.open(archive) as source:
            source.extractall(temporary, filter="data")
        extracted = Path(temporary) / bundle["name"]
        files = file_sums(extracted)
        if confirmed != "pinned":
            if files == bundle["files"]:
                print(f"{bundle['name']}: the archive was rebuilt upstream, and its files are still the pinned ones")
            elif confirmed == "published":
                print(f"WARN {bundle['name']} has changed upstream: installed the new version, "
                      "which matches the checksum its publisher lists")
            else:
                print(f"WARN {bundle['name']} has changed upstream and its publisher lists no checksum, "
                      "so the new version was installed unverified")
        if folder.exists():
            folder.rename(Path(temporary) / "replaced")
        extracted.rename(folder)
    return files


def install(root, bundles=None):
    """Install each selected bundle that is missing or damaged, then record what was installed."""
    manifest = json.loads((Path(__file__).resolve().parents[1] / "voice/models.json").read_text())
    selected = set(bundles) if bundles is not None else {bundle["name"] for bundle in manifest["bundles"]}
    if unknown := selected - {bundle["name"] for bundle in manifest["bundles"]}:
        raise ValueError("Unknown speech bundles: " + ", ".join(sorted(unknown)))
    root.mkdir(parents=True, exist_ok=True)
    for bundle in manifest["bundles"]:
        if bundle["name"] not in selected:
            continue
        folder, record = root / bundle["name"], root / f".{bundle['name']}.files.json"
        if folder.exists():
            try:
                expected = json.loads(record.read_text())
            except (OSError, ValueError):
                expected = None
            if not isinstance(expected, dict):
                expected = bundle["files"]
            if not (wrong := mismatched(folder, expected)):
                if not record.exists():
                    record.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
                print("Verified", bundle["name"])
                continue
            print(f"{bundle['name']}: {', '.join(wrong[:5])}{' and more' if len(wrong) > 5 else ''} "
                  "differ from what was installed; downloading it again")
        files = fetch(bundle, root)
        record.write_text(json.dumps(files, indent=2, sort_keys=True) + "\n")
        print("Installed", bundle["name"])


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
