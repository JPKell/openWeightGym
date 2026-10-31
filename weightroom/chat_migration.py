"""Copy only web-chat records to front, verify them, then retire compute's chat tables.

Export reads a consistent SQLite snapshot. Retirement checks every copied value inside a
write transaction before dropping the three chat tables. Neither operation exposes text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from db import connect

from weightroom.chat_store import Conversations
from loadcoach.users import keep_private

TABLES = ("conversations", "messages", "answering")


def records(db: sqlite3.Connection, translate: bool = False) -> dict:
    """Read chat-only records and sequence counters, translating legacy owner names."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not set(TABLES) <= tables:
        raise ValueError("The source does not have all three chat tables.")
    identities = {}
    if translate:
        for table in ('collections', 'documents', 'document_chunks', 'prompt_versions', 'workflows', 'workflow_attempts'):
            if table in tables and db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                raise ValueError('The legacy chat-only migration cannot move document collections, prompt versions or workflows. Use the full store backup/transfer workflow.')
        if "identity" not in {row[1] for row in db.execute("PRAGMA table_info(users)")}:
            raise ValueError("Upgrade LoadCoach first so accounts have stable identities.")
        identities = dict(db.execute("SELECT name, identity FROM users"))
    result = {}
    for table in TABLES:
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
        rows = [list(row) for row in db.execute(f"SELECT * FROM {table}")]
        if table == "conversations":
            for column, default in (("session_mode", "fresh"), ("sessions", "{}")):
                if column not in columns:
                    columns.append(column)
                    for row in rows:
                        row.append(default)
        if translate and "user" in columns:
            index = columns.index("user")
            for row in rows:
                name = row[index]
                # Unowned historical records must never become a newly created user's.
                row[index] = identities.get(name) or (name if name in ("anonymous", "command-line")
                    else "unclaimed:" + hashlib.sha256(name.encode()).hexdigest())
        result[table] = {"columns": columns, "rows": sorted(rows, key=lambda row: json.dumps(row))}
    result["sequences"] = dict(db.execute(
        "SELECT name, seq FROM sqlite_sequence WHERE name IN ('conversations', 'messages')"
    ))
    return result


def digest(record: dict) -> str:
    """Hash exact copied values without printing private conversation content."""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def export(source: Path, destination: Path) -> dict:
    """Create a new private, chat-only database without changing the source."""
    if destination.exists():
        raise ValueError("Destination already exists; use verify instead of overwriting it.")
    with connect(source.resolve().as_uri() + "?mode=ro", uri=True) as origin:
        origin.execute("BEGIN")
        data = records(origin, translate=True)
        if data["answering"]["rows"]:
            raise ValueError("Chat turns are active. Finish them before migrating.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    target = None
    try:
        target = Conversations(str(destination))
        with target.db:
            for table in TABLES:
                columns, rows = data[table]["columns"], data[table]["rows"]
                expected = {row[1] for row in target.db.execute(f"PRAGMA table_info({table})")}
                if not set(columns) <= expected:
                    raise ValueError(f"Unsupported source columns in {table}.")
                if rows:
                    target.db.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", rows)
            for table, seq in data["sequences"].items():
                target.db.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
                target.db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq))
        copied = records(target.db)
        if copied != data:
            raise ValueError("Copied data differs from the source; nothing has been retired.")
        if target.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("The exported database failed its integrity check.")
        return {"counts": {table: len(data[table]["rows"]) for table in TABLES}, "sha256": digest(data)}
    except BaseException:
        if target:
            target.db.close()
            target = None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if target:
            target.db.close()
        keep_private(str(destination))


def verify(source: Path, destination: Path) -> dict:
    """Compare every exported chat value and preserved ID sequence with compute."""
    with connect(source.resolve().as_uri() + "?mode=ro", uri=True) as origin, connect(
        destination.resolve().as_uri() + "?mode=ro", uri=True
    ) as target:
        origin.execute("BEGIN")
        target.execute("BEGIN")
        data = records(origin, translate=True)
        if data != records(target):
            raise ValueError("Source and destination differ; retirement is refused.")
        return {"counts": {table: len(data[table]["rows"]) for table in TABLES}, "sha256": digest(data)}


def retire(source: Path, verified_copy: Path) -> dict:
    """Remove only the chat tables after comparing them to an independently kept copy."""
    with connect(verified_copy.resolve().as_uri() + "?mode=ro", uri=True) as archive:
        archive.execute("BEGIN")
        copied = records(archive)
    with connect(source.resolve().as_uri() + "?mode=rw", uri=True) as origin:
        origin.execute("BEGIN IMMEDIATE")
        data = records(origin, translate=True)
        if data != copied or data["answering"]["rows"]:
            raise ValueError("Chat records changed or work is active; retirement is refused.")
        for table in reversed(TABLES):
            origin.execute(f"DROP TABLE {table}")
    return {"retired": list(TABLES), "sha256": digest(copied)}


def main() -> None:
    """Run an explicit export, verification, or verified retirement operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "verify", "retire"))
    parser.add_argument("source", type=Path)
    parser.add_argument("copy", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps({"export": export, "verify": verify, "retire": retire}[args.action](args.source, args.copy)))
    except (ValueError, sqlite3.Error, OSError) as problem:
        parser.exit(1, f"Migration stopped: {problem}\n")


if __name__ == "__main__":
    main()
