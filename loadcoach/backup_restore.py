"""Offline recovery checks and private staging; never replace a running service's stores.

Run ``python -m loadcoach.backup_restore --help`` on a trusted recovery machine.
The identity must be a private age identity file, supplied from separate recovery storage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from .backup_crypto import decrypt, read_regular, safe_path
from .backups import _check_payload, _safe_extract, _sha256


def verify(archive: Path, identity: Path, *, output: Path | None = None,
           max_bytes: int = 1024**4, expected_sha256: str | None = None) -> dict:
    """Authenticate before extraction, validate embedded inventory, optionally publish fresh staging."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("The recovery size limit must be a positive byte count.")
    archive, identity = safe_path(archive), safe_path(identity)
    if not archive.is_file() or archive.stat().st_size > max_bytes:
        raise ValueError("The encrypted recovery archive is missing or exceeds the size limit.")
    if expected_sha256 is not None and _sha256(archive) != expected_sha256:
        raise ValueError("The encrypted archive does not match the independently saved SHA-256.")
    if output is not None:
        output = safe_path(output)
        if output.exists() or not output.parent.is_dir():
            raise ValueError("Choose a new staging directory beneath an existing trusted directory.")
    with tempfile.TemporaryDirectory(prefix="suite-recovery-", dir=output.parent if output else None) as temporary:
        work = Path(temporary)
        plaintext = work / "archive.tar.gz"
        decrypt(archive, identity, plaintext)
        staged = work / "staged"
        staged.mkdir(mode=0o700)
        _safe_extract(plaintext, staged, max_bytes)
        manifest = json.loads(read_regular(staged / "manifest.json", 16 * 1024**2))
        counts = _check_payload(staged / "payload", manifest)
        # PostgreSQL dumps are checked byte-for-byte here; the creation-time native
        # restore check is historical evidence. An actual recovery drill must restore
        # each dump to a disposable server and exercise the application.
        report = {"id": manifest["id"], "machine": manifest["machine"],
                  "archive_sha256": _sha256(archive), "authenticated": True,
                  "recovery_key_tested": True, "files": len(manifest["files"]),
                  "counts": counts, "live_data_changed": False,
                  "postgresql_restore_required": any(store["engine"] == "postgresql"
                                                       for store in manifest.get("stores", []))}
        if output is not None:
            # Refuse collisions instead of replacing an existing empty directory.
            output.mkdir(mode=0o700)
            try:
                for child in staged.iterdir():
                    shutil.move(str(child), output / child.name)
            except BaseException:
                shutil.rmtree(output)
                raise
            report["staged"] = str(output)
        return report


def main(argv: list[str] | None = None) -> None:
    """Verify or stage an encrypted archive without replacing any live store."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Encrypted archive.tar.gz.age, never a legacy plaintext archive")
    parser.add_argument("--identity", type=Path, required=True, help="Private age identity file on the recovery machine")
    parser.add_argument("--output", type=Path, help="New private staging directory; omit for a temporary verification only")
    parser.add_argument("--max-bytes", type=int, default=1024**4, help="Maximum archive and extracted bytes (default 1 TiB)")
    parser.add_argument("--sha256", help="Expected ciphertext hash from an independent trusted record")
    args = parser.parse_args(argv)
    try:
        report = verify(args.archive, args.identity, output=args.output,
                        max_bytes=args.max_bytes, expected_sha256=args.sha256)
    except Exception:
        # Never print subprocess diagnostics, keys, database errors or decrypted metadata.
        parser.exit(1, "Recovery failed; check the archive, identity, permissions, size limit and fresh staging path. Live data was not changed.\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
