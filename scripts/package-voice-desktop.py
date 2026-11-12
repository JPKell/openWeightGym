"""Create a small cross-platform source download with launchers and no server credentials."""

import argparse
from pathlib import Path
import tempfile
import zipfile


def package(destination):
    """Package an explicit file list; local settings, keys and recordings cannot enter it."""
    root = Path(__file__).resolve().parents[1]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as work:
        staged = Path(work) / destination.name
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("__init__.py", "desktop.py", "desktop_audio.py", "echo.py", "protocol.py"):
                archive.write(root / "voice" / name, "LoadCoach-Voice/voice/" + name)
            for name in ("requirements.txt", "README.txt", "Voice.cmd", "Voice.command"):
                source = root / "desktop" / name
                info = zipfile.ZipInfo.from_file(source, "LoadCoach-Voice/" + name)
                if name == "Voice.command":
                    info.external_attr = 0o100755 << 16
                archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        staged.replace(destination)
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("voice-test/distribution/LoadCoach-Voice.zip"))
    package(parser.parse_args().output)
