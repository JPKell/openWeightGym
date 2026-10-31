"""Guarded, idempotent DB1 history imports; source snapshots are never changed.

The CLI defaults to preview. Apply only inside the DB1 operating gates, with a
verified recovery set. It adds missing records and refuses conflicting records.
"""

import argparse
import json
import math
from pathlib import Path
import re

from db import DatabaseRegistry
from db.consolidation import normalized, readonly, quoted
from loadcoach.config import data_folder, read_settings


def telemetry(source, target, fingerprint, *, apply=False):
    """Import retained machine telemetry into front-owned history."""
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("A verified physical host fingerprint is required")
    if not target.execute("SELECT 1 FROM machine_hosts WHERE fingerprint=?", (fingerprint,)).fetchone():
        raise ValueError("The verified physical host must already be known on front")
    readings = source.execute("SELECT at,numbers FROM readings ORDER BY at").fetchall()
    additions, unchanged = [], 0
    for at, numbers in readings:
        values = json.loads(numbers)
        if not isinstance(at, (int, float)) or not math.isfinite(at) or not isinstance(values, dict) or any(
            not isinstance(key, str) or value is not None and
            (type(value) not in (int, float) or not math.isfinite(value)) for key, value in values.items()
        ):
            raise ValueError("History contains an invalid timestamp or numeric series")
        found = target.execute("SELECT numbers FROM machine_telemetry WHERE fingerprint=? AND at=?", (fingerprint, at)).fetchone()
        if found:
            if normalized(found[0], "numbers") != values:
                raise ValueError("History conflicts with an existing host/timestamp; nothing imported")
            unchanged += 1
        else:
            additions.append((fingerprint, at, numbers))
    before = target.execute("SELECT COUNT(*) FROM machine_telemetry WHERE fingerprint=?", (fingerprint,)).fetchone()[0]
    if apply:
        with target:
            target.executemany("INSERT INTO machine_telemetry(fingerprint,at,numbers) VALUES(?,?,?) ON CONFLICT DO NOTHING", additions)
            # Verify every source record after insertion, including a competing writer.
            for at, numbers in readings:
                found = target.execute("SELECT numbers FROM machine_telemetry WHERE fingerprint=? AND at=?", (fingerprint, at)).fetchone()
                if not found or normalized(found[0], "numbers") != normalized(numbers, "numbers"):
                    raise ValueError("Imported history did not verify; transaction rolled back")
    return {"source": len(readings), "before": before, "missing": len(additions), "identical": unchanged,
            "after": target.execute("SELECT COUNT(*) FROM machine_telemetry WHERE fingerprint=?", (fingerprint,)).fetchone()[0],
            "source_first": readings[0][0] if readings else None, "source_last": readings[-1][0] if readings else None,
            "applied": apply}


def reliability(source, accounts, target, *, machine="compute", apply=False):
    """Import retained model reliability outcomes into front storage."""
    cursor = source.execute("SELECT * FROM reliability_outcomes ORDER BY request")
    names = [one[0] for one in cursor.description]
    additions, unchanged = [], 0
    for values in cursor:
        row = dict(zip(names, values))
        found = target.execute("SELECT " + ",".join(map(quoted, names)) + " FROM reliability_outcomes WHERE request=?", (row["request"],)).fetchone()
        if found:
            if tuple(found) != tuple(values):
                raise ValueError("Reliability request conflicts with front; nothing imported")
            unchanged += 1
            continue
        owner = accounts.execute("SELECT name FROM users WHERE identity=?", (row["owner_id"],)).fetchone()
        event = accounts.execute("SELECT user,model,detail FROM events WHERE machine=? AND source=? AND kind='request' AND what='asked'",
                                 (machine, row["request"])).fetchall()
        if not owner or len(event) != 1 or event[0][0] != row["user"] or event[0][1] != row["model"] or json.loads(event[0][2]).get("model_id") != row["model_id"]:
            raise ValueError("Missing reliability outcome has unverified owner/request/model attribution")
        additions.append(tuple(values))
    before = target.execute("SELECT COUNT(*) FROM reliability_outcomes").fetchone()[0]
    if apply:
        with target:
            target.executemany("INSERT INTO reliability_outcomes(" + ",".join(map(quoted, names)) + ") VALUES(" + ",".join("?" for _ in names) + ") ON CONFLICT(request) DO NOTHING", additions)
            for values in additions:
                found = target.execute("SELECT " + ",".join(map(quoted, names)) + " FROM reliability_outcomes WHERE request=?", (values[names.index("request")],)).fetchone()
                if not found or tuple(found) != values:
                    raise ValueError("Imported reliability did not verify; transaction rolled back")
    return {"source": len(additions) + unchanged, "before": before, "missing": len(additions), "identical": unchanged,
            "after": target.execute("SELECT COUNT(*) FROM reliability_outcomes").fetchone()[0], "applied": apply}


def copy_missing(source, target, selections, *, apply=False):
    """One transaction for a consistency-bound collection, with content collision checks."""
    plans, reports = [], {}
    for table, where, parameters, additions in selections:
        info = source.execute(f'PRAGMA table_info({quoted(table)})').fetchall()
        keys = [one[1] for one in sorted(info, key=lambda one: one[5]) if one[5]]
        if not keys:
            raise ValueError('Imports require a primary key for every table')
        cursor = source.execute(f'SELECT * FROM {quoted(table)} {where}', parameters)
        original_names = [one[0] for one in cursor.description]
        if set(additions) & set(original_names):
            raise ValueError('This import expects an unmodified legacy source schema')
        names = original_names + list(additions)
        values = []
        for row in cursor:
            original = dict(zip(original_names, row))
            values.append(tuple(row) + tuple(value(original) if callable(value) else value for value in additions.values()))
        predicate = ' AND '.join(quoted(key) + '=?' for key in keys)
        selected = ','.join(map(quoted, names))
        missing = []
        for row in values:
            key = tuple(row[names.index(key)] for key in keys)
            found = target.execute(f'SELECT {selected} FROM {quoted(table)} WHERE {predicate}', key).fetchone()
            if found is None:
                missing.append(row)
            elif any(normalized(left, column) != normalized(right, column) for column, left, right in zip(names, row, found)):
                raise ValueError(f'{table} has a conflicting destination key; nothing imported')
        reports[table] = {'source': len(values), 'missing': len(missing), 'identical': len(values) - len(missing),
                          'before': target.execute(f'SELECT COUNT(*) FROM {quoted(table)}').fetchone()[0]}
        plans.append((table, names, keys, values, missing))
    if apply:
        with target:
            for table, names, keys, values, missing in plans:
                target.executemany(f'INSERT INTO {quoted(table)}(' + ','.join(map(quoted, names)) + ') VALUES(' + ','.join('?' for _ in names) + ') ON CONFLICT DO NOTHING', missing)
                predicate = ' AND '.join(quoted(key) + '=?' for key in keys)
                for row in values:
                    key = tuple(row[names.index(key)] for key in keys)
                    found = target.execute('SELECT ' + ','.join(map(quoted, names)) + f' FROM {quoted(table)} WHERE {predicate}', key).fetchone()
                    if found is None or any(normalized(left, column) != normalized(right, column) for column, left, right in zip(names, row, found)):
                        raise ValueError(f'{table} failed post-import comparison; transaction rolled back')
    for table in reports:
        reports[table]['after'] = target.execute(f'SELECT COUNT(*) FROM {quoted(table)}').fetchone()[0]
    return {'applied': apply, 'tables': reports}


def tasks(source, target, *, machine='compute', apply=False):
    """Preserve terminal legacy task/batch/file history, including existing retention holes."""
    if source.execute("SELECT 1 FROM batches WHERE endpoint='/loadcoach/task' AND status NOT IN ('completed','cancelled') LIMIT 1").fetchone():
        raise ValueError('Legacy tasks must all be terminal before the offline history move')
    if source.execute("SELECT 1 FROM external_tasks e LEFT JOIN batches b ON b.id=e.id WHERE b.id IS NULL OR b.endpoint<>'/loadcoach/task' LIMIT 1").fetchone():
        raise ValueError('Every external task needs its owning task batch')
    known_tables = {name for name, in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    owners = dict(source.execute('SELECT name,identity FROM users')) if 'users' in known_tables else {}
    batch_users = dict(source.execute('SELECT id,user FROM batches'))
    def owner_id(row):
        """Resolve a legacy task owner to its stable front account identity."""
        return json.loads(row['subject']).get('owner_id') or owners.get(batch_users[row['id']])
    # Preserve all source columns exactly; immutable identity is added in a front-only
    # column, so a removed and recreated username cannot inherit legacy task history.
    return copy_missing(source, target, [
        ('files', "WHERE id IN (SELECT input_file FROM batches WHERE endpoint='/loadcoach/task' UNION SELECT output_file FROM batches WHERE endpoint='/loadcoach/task' UNION SELECT error_file FROM batches WHERE endpoint='/loadcoach/task')", (), {}),
        ('batches', "WHERE endpoint='/loadcoach/task'", (), {}),
        ('batch_lines', "WHERE batch IN (SELECT id FROM batches WHERE endpoint='/loadcoach/task')", (), {}),
        ('external_tasks', '', (), {'machine': machine, 'owner_id': owner_id}),
    ], apply=apply)


def settings(source, target, *, apply=False, programs=None):
    """Import front-program setting overrides from the legacy store."""
    from turnstile.settings import PROGRAMS
    names = tuple(sorted(PROGRAMS if programs is None else programs))
    where = 'WHERE program IN (' + ','.join('?' for _ in names) + ')'
    return copy_missing(source, target, [(table, where, names, {}) for table in ('settings_changed', 'settings_seen')], apply=apply)


def grants(source, target, *, apply=False):
    """Import existing schedule authorization grants into front ownership."""
    return copy_missing(source, target, [('schedule_grants', '', (), {})], apply=apply)


def main():
    """Run the guarded ownership import for the selected data category."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("telemetry", "reliability", "tasks", "settings", "grants"))
    parser.add_argument("source", type=Path, help="Offline source SQLite snapshot")
    parser.add_argument("--config", type=Path, default=Path("settings/weightroom.toml"))
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--machine", default="compute")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    told = read_settings(args.config)
    folder = data_folder(args.config, told)
    stores = DatabaseRegistry.from_config(told.get("databases"), args.config.parent,
        {f"weightroom.{name}": folder / f"{name}.db" for name in ("accounts", "roles")})
    accounts = stores.database("weightroom.accounts").connect()
    roles = None
    try:
        with readonly(args.source) as source:
            if args.kind == "telemetry":
                result = telemetry(source, accounts, args.fingerprint, apply=args.apply)
            elif args.kind == 'reliability':
                roles = stores.database("weightroom.roles").connect()
                result = reliability(source, accounts, roles, machine=args.machine, apply=args.apply)
            elif args.kind == 'tasks':
                result = tasks(source, accounts, machine=args.machine, apply=args.apply)
            elif args.kind == 'settings':
                result = settings(source, accounts, apply=args.apply)
            else:
                result = grants(source, accounts, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
    finally:
        accounts.close()
        if roles is not None:
            roles.close()


if __name__ == "__main__":
    main()
