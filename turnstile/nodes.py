"""The compute nodes the control node knows, all peers (docs/peer-nodes-plan.md, Phase 2).

Every node is a `[machines.NAME]` table. An install that still has the old top-level `compute`
address has it read as one more node, named `LEGACY`, until `owg update` moves it to
`[machines.<hostname>]` with `formerly = "compute"`. That line records an alias: a stored row,
request number or signed statement that still says `compute` means the renamed node.

Stored names are rewritten to the new name only after a verified backup of the control node's stores
(`rename_stored`); until then, and for hash-chained values that are never rewritten, readers
resolve the old name through the alias.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from loadcoach.log import LEGACY  # the old default node's name, kept only to read older settings and rows
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
BACKUP_FRESH_DAYS = 7  # how recent a verified control node backup must be before stored names are rewritten
# Where stored rows name a node, by store. Each is (table, column). `benchmark_runs.location`
# also has '' for the old default node.
STORED = {
    "weightroom.accounts": (("events", "machine"), ("machine_lifecycle", "name"), ("machine_key_rotations", "machine"),
                            ("observation_spans", "machine"), ("observation_sources", "machine"),
                            ("observation_metrics", "machine"), ("benchmark_tasks", "machine")),
    "weightroom.roles": (("model_locations", "machine"),),
    "weightroom.benchmarks": (("benchmark_runs", "location"),),
    "loadcoach.core": (("events", "machine"), ("observation_spans", "machine"),
                       ("observation_sources", "machine"), ("observation_metrics", "machine"),
                       ("benchmark_tasks", "machine")),
}


def valid_name(name: str) -> bool:
    """Whether `name` may name a renamed node: lower-case letters, digits and dashes, or an identifier; never `front` or the old name."""
    return bool(NAME.fullmatch(name) or name.isidentifier()) and name not in ("front", LEGACY)


def legacy_entry(host: str, ports: tuple[int, ...], offset: int, key_file: str, wake_mac: str, wake_address: str) -> dict:
    """The `[machines.NAME]` entry the old top-level `compute` host stands for."""
    return {"compute": f"http://{host}:{ports[0] + offset}",
            "spotter": f"http://{host}:{ports[2] + offset}" if len(ports) > 2 else "",
            "machine_key_file": key_file, "wake_mac": wake_mac, "wake_address": wake_address or "255.255.255.255"}


def wake_policy(name, entry, changes):
    """Lay only catalogue-approved Wake overrides over one configured node."""
    from loadcoach.settings import live
    return {**entry, **{field: live('machines:' + name, field, entry.get(field, fallback), changes=changes)
                       for field, fallback in (('wake_mac', ''), ('wake_address', '255.255.255.255'))}}


def base(entry: dict, port: int, ports: tuple[int, ...]) -> str:
    """Where a node answers on one of the control node's `ports`.

    LoadCoach's port is its `compute` URL and Spotter's its `spotter` URL. Any other port (voice)
    is on the same host, as far from the `compute` URL's port as it is from LoadCoach's own.
    """
    url = entry["compute"].rstrip("/")
    if port == ports[0]:
        return url
    if len(ports) > 2 and port == ports[2] and entry.get("spotter"):
        return entry["spotter"].rstrip("/")
    parts = urlsplit(url)
    offset = (parts.port or ports[0]) - ports[0]
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    return f"{parts.scheme}://{host}:{port + offset}"


def aliases_from(machines: dict) -> dict[str, str]:
    """{old name: node} from the `formerly` lines of the configured nodes."""
    aliases = {}
    for name, entry in machines.items():
        old = entry.get('formerly')
        if not old or old == name:
            continue
        if old in machines or old in aliases:
            raise ValueError(f'Former node name {old!r} is ambiguous; each alias must identify one peer.')
        aliases[old] = name
    return aliases



def remember_aliases(db, told: dict[str, str]) -> dict[str, str]:
    """Record new aliases in `node_aliases` and give every one the store knows."""
    for old, new in told.items():
        previous = db.execute('SELECT new FROM node_aliases WHERE old=?', (old,)).fetchone()
        if previous and previous[0] != new:
            raise ValueError(f'Node alias {old!r} already identifies {previous[0]!r}; refusing to redirect historical links to {new!r}.')
        db.execute("INSERT INTO node_aliases(old, new, at) VALUES (?, ?, ?) ON CONFLICT(old) DO UPDATE SET new=excluded.new "
                   "WHERE node_aliases.new <> excluded.new", (old, new, time.time()))
    return {old: new for old, new in db.execute("SELECT old, new FROM node_aliases")}


def verified_backup(data: str | Path, now: float | None = None, machine: str = "front") -> dict | None:
    """The newest verified backup of the control node's own stores, if it is recent enough to rewrite stored names after."""
    from loadcoach import backups
    root = backups.backup_root(data)
    if not root.is_dir():
        return None
    now = time.time() if now is None else now
    try:
        found = backups.manifests(root)
    except (OSError, ValueError):
        return None
    def created(manifest):
        value = manifest.get('created_at', 0)
        try:
            return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0
    for manifest in sorted(found, key=created, reverse=True):
        verification = manifest.get("verification") or {}
        if (verification.get("state") == "verified" and manifest.get("machine") in (machine, LEGACY if machine != "front" else "front")
                and 0 <= now - created(manifest) <= BACKUP_FRESH_DAYS * 86400):
            try:
                with (root / manifest['id'] / backups.ARCHIVE).open('rb') as archive:
                    digest = hashlib.file_digest(archive, 'sha256').hexdigest()
                if digest == manifest.get('archive_sha256'):
                    return manifest
            except (OSError, KeyError, ValueError):
                continue
    return None


def rename_stored(store: str, db, aliases: dict[str, str], backup: dict | None, say=print) -> int:
    """Rename references transactionally in a numbered migration after verified backup.

    A conflict stops the migration and leaves every original row intact. Hash-chained
    event details are never edited; aliases keep their historical names readable.
    """
    if not aliases or backup is None:
        return 0
    from db import Migration, migrate
    from db.migrations import columns
    from db.schemas import PEER_NODE_REFERENCES
    source = db.database
    changed = 0
    def rename(database, connection):
        nonlocal changed
        tables = set(database.tables(connection))
        for old, new in aliases.items():
            for table, column in STORED.get(store, ()):
                if table not in tables or column not in columns(database, connection, table):
                    continue
                # Both names may already have observations when an older store has
                # been running alongside its renamed peer. Keep the newest source
                # report and add metric buckets rather than losing either history.
                if table == 'observation_sources':
                    connection.execute('''INSERT INTO observation_sources(machine,at,data)
                        SELECT ?,at,data FROM observation_sources WHERE machine=?
                        ON CONFLICT(machine) DO UPDATE SET at=excluded.at,data=excluded.data
                        WHERE excluded.at > observation_sources.at''', (new, old))
                    connection.execute('DELETE FROM observation_sources WHERE machine=?', (old,))
                    continue
                if table == 'observation_metrics':
                    connection.execute('''INSERT INTO observation_metrics
                        SELECT minute,owner_id,service,?,operation,outcome,benchmark,metric,bucket,count,total
                        FROM observation_metrics WHERE machine=?
                        ON CONFLICT(minute,owner_id,service,machine,operation,outcome,benchmark,metric,bucket)
                        DO UPDATE SET count=observation_metrics.count+excluded.count,
                                      total=observation_metrics.total+excluded.total''', (new, old))
                    connection.execute('DELETE FROM observation_metrics WHERE machine=?', (old,))
                    continue
                values = (old, '') if table == 'benchmark_runs' and old == LEGACY else (old,)
                for value in values:
                    cursor = connection.execute(f'UPDATE {table} SET {column} = ? WHERE {column} = ?', (new, value))
                    changed += max(cursor.rowcount or 0, 0)
            if 'machine_hosts' in tables:
                for fingerprint, names in list(connection.execute('SELECT fingerprint,names FROM machine_hosts')):
                    renamed = list(dict.fromkeys(aliases.get(name, name) for name in json.loads(names)))
                    if renamed != json.loads(names):
                        connection.execute('UPDATE machine_hosts SET names=? WHERE fingerprint=?', (json.dumps(renamed), fingerprint))
                        changed += 1
    step = PEER_NODE_REFERENCES[0]
    migrate(source, db, 'peer_nodes.' + store, (Migration(step.version, step.name, rename),))
    if changed:
        say(f"turnstile: renamed {changed} stored node references in {store} after backup {backup.get('id', '')}", flush=True)
    return changed
