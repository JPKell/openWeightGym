"""Explicit offline DB1 retirement, bound to verified recovery and exact row evidence."""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import time

from db import Database, local_path, migrate, schema_version
from db.consolidation import audit, digest, normalized, readonly, rows, tables
from db.schemas import COMPUTE_RETIREMENT, RETIRED_COMPUTE_TABLES, stored_table


def checksum(path):
    """Stream the file into a SHA-256 checksum."""
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stamp(connection, table):
    """Return the retained store's file size and modification timestamp."""
    present = tables(connection)
    table = stored_table(present, table)  # a snapshot from before the benchmark task tables were renamed
    if table not in present:
        return None
    values = rows(connection, table)
    return {'rows': len(values), 'sha256': digest(sorted(
        digest({key: normalized(value, key) for key, value in row.items()}) for row in values))}


def recovery_assets(folder, outside, identity=None):
    """Bind both verified archive halves and independent database copies to the proof."""
    folder, outside = Path(folder).resolve(), Path(outside).resolve()
    assets, snapshots, identifiers = {}, {}, set()
    for side, names in [('compute', ('loadcoach', 'spotter')), ('front', ('accounts', 'roles', 'benchmarks'))]:
        manifest_path, archive = folder / side / 'manifest.json', folder / side / 'archive.tar.gz'
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('machine') != side or manifest.get('verification', {}).get('state') != 'verified':
            raise ValueError('Both recovery halves must have verified isolated restores')
        identifiers.add(manifest['id'])
        encrypted = archive.with_suffix(archive.suffix + '.age')
        expected_hashes = {}
        if encrypted.is_file():
            if identity is None:
                raise ValueError('Encrypted retirement recovery requires an offline age identity')
            from loadcoach.backup_restore import verify
            with tempfile.TemporaryDirectory(prefix='suite-retirement-check-') as temporary:
                staged = Path(temporary) / 'staged'
                verify(encrypted, Path(identity), output=staged, expected_sha256=manifest.get('archive_sha256'))
                embedded = json.loads((staged / 'manifest.json').read_text())
                if embedded != {key: value for key, value in manifest.items() if key != 'archive_sha256'}:
                    raise ValueError('The recovery sidecar differs from its authenticated manifest')
                expected_hashes = {name: checksum(staged / 'payload/data' / (name + '.db')) for name in names}
            archive = encrypted
        else:
            with tarfile.open(archive) as saved:
                for name in names:
                    with saved.extractfile('payload/data/' + name + '.db') as source:
                        expected_hashes[name] = hashlib.file_digest(source, 'sha256').hexdigest()
        for path in (manifest_path, archive):
            assets[str(path)] = checksum(path)
        for name, expected in expected_hashes.items():
            snapshot = folder / (name + '.db')
            if checksum(snapshot) != expected:
                raise ValueError('A comparison snapshot differs from its verified recovery archive')
            snapshots[name] = snapshot
            if side == 'compute':
                independent = outside / (name + '.db')
                if checksum(independent) != expected:
                    raise ValueError('Independent compute recovery copies do not match')
                assets[str(independent)] = expected
    if len(identifiers) != 1:
        raise ValueError('Recovery halves must belong to the same paired set')
    retention = json.loads((outside / 'retention.json').read_text())
    if not retention.get('owner_instruction_required') or retention.get('backup_id') not in identifiers:
        raise ValueError('Independent copies need their owner-controlled retention record')
    assets[str(outside / 'retention.json')] = checksum(outside / 'retention.json')
    return assets, snapshots, identifiers.pop()


def prepare(target, recovery, outside, approved_audit, identity=None):
    """Refuse new missing/conflicting data; accept only the owner's exact archived exceptions."""
    target, outside = Path(target).resolve(), Path(outside).resolve()
    project = target.parent.parent
    if outside == project or project in outside.parents:
        raise ValueError('Independent recovery copies must be outside the project')
    assets, snapshots, identifier = recovery_assets(recovery, outside, identity)
    previous = json.loads(Path(approved_audit).read_text())
    approved = {(table, record['key_sha256'], record['content_sha256'], record['state'])
                for table, found in previous['tables'].items() for record in found['records']
                if record['state'] == 'different' or table == 'keys' and record['state'] == 'only_compute'}
    compared = audit(snapshots['loadcoach'], snapshots['accounts'], snapshots['roles'], snapshots['benchmarks'])
    with readonly(snapshots['loadcoach']) as source:
        expired = {digest([key]) for key, expires in source.execute('SELECT hash,expires FROM keys')
                   if expires is not None and expires < time.time()} if 'keys' in tables(source) else set()
        for table in RETIRED_COMPUTE_TABLES:
            for record in compared['tables'].get(table, {}).get('records', []):
                if record['state'] == 'identical':
                    continue
                exception = (table, record['key_sha256'], record['content_sha256'], record['state'])
                if exception not in approved or record['state'] == 'only_compute' and (
                    table != 'keys' or record['key_sha256'] not in expired
                ):
                    raise ValueError(f'{table}: unmoved or newly conflicting data prevents retirement')
        fingerprints = {table: stamp(source, table) for table in RETIRED_COMPUTE_TABLES}
    return {'schema': 1, 'target': str(target), 'recovery_set': identifier,
            'assets': assets, 'approved_audit_sha256': checksum(approved_audit),
            'tables': fingerprints}


def retire(target, proof, *, apply=False):
    """Validate inside the migration transaction; never apply on service startup."""
    target = Path(target).resolve()
    if proof.get('schema') != 1 or proof.get('target') != str(target) or set(proof.get('tables', {})) != set(RETIRED_COMPUTE_TABLES):
        raise ValueError('This proof does not authorize this exact store/table set')
    for path, expected in proof['assets'].items():
        if checksum(path) != expected:
            raise ValueError('A verified recovery asset changed; retirement refused')
    # A service or even a retained reader must close its store before this command.
    with readonly(target) as source:
        already = schema_version(source, 'loadcoach.retirement')
        if already:
            if set(RETIRED_COMPUTE_TABLES) & set(tables(source)):
                raise ValueError('A retired store has recreated legacy tables')
            return {'state': 'already_retired', 'recovery_set': proof['recovery_set']}
        if any(stamp(source, table) != expected for table, expected in proof['tables'].items()):
            raise ValueError('Compute data changed after verification; repeat the recovery/comparison gate')
    if not apply:
        return {'state': 'preview', 'tables': proof['tables'], 'recovery_set': proof['recovery_set']}
    database, before = Database.sqlite(target), target.stat().st_size
    connection = database.connect(timeout=0)
    try:
        def guarded(database, connection):
            """Verify the recovery and acceptance conditions before retiring a table."""
            if any(stamp(connection, table) != expected for table, expected in proof['tables'].items()):
                raise ValueError('Compute data changed before the migration lock was acquired')
            if 'batches' in tables(connection) and connection.execute(
                "SELECT 1 FROM batches WHERE status NOT IN ('completed','cancelled') LIMIT 1"
            ).fetchone():
                raise ValueError('Nonterminal compute work prevents retirement')
            kept = {table: stamp(connection, table) for table in tables(connection)
                    if table not in RETIRED_COMPUTE_TABLES and table != 'suite_schema_migrations'}
            COMPUTE_RETIREMENT[0].body(database.engine)(database, connection)
            if any(stamp(connection, table) != expected for table, expected in kept.items()):
                raise ValueError('Retained compute data changed; migration rolled back')
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Retirement would leave broken references')
        migrate(database, connection, 'loadcoach.retirement', (replace(COMPUTE_RETIREMENT[0], sqlite=guarded),))
        connection.execute('VACUUM')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Post-retirement SQLite integrity check failed; use the verified recovery copy')
    finally:
        connection.close()
    return {'state': 'retired', 'tables': list(RETIRED_COMPUTE_TABLES),
            'before_bytes': before, 'after_bytes': target.stat().st_size,
            'recovery_set': proof['recovery_set']}


def main():
    """Run the guarded retirement of verified obsolete compute tables."""
    from loadcoach.config import kept_by_loadcoach
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'preview', 'apply'))
    parser.add_argument('--config', type=Path, default=Path('settings/loadcoach.toml'))
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--recovery', type=Path)
    parser.add_argument('--outside', type=Path)
    parser.add_argument('--approved-audit', type=Path)
    parser.add_argument('--identity', type=Path, help='Offline age identity for encrypted recovery sets')
    args = parser.parse_args()
    told, store, _ = kept_by_loadcoach(args.config)
    target = local_path(store)
    if not told.get('front') or target is None or not target.is_file():
        parser.error('A compute configuration with a front address and an existing SQLite compute store are required')
    if args.action == 'prepare':
        if not all((args.recovery, args.outside, args.approved_audit)):
            parser.error('prepare needs --recovery, --outside and --approved-audit')
        result = prepare(target, args.recovery, args.outside, args.approved_audit, args.identity)
        args.proof.write_text(json.dumps(result, indent=2) + '\n')
        args.proof.chmod(0o600)
        print(json.dumps({'state': 'prepared', 'recovery_set': result['recovery_set'], 'tables': result['tables']}))
    else:
        print(json.dumps(retire(target, json.loads(args.proof.read_text()), apply=args.action == 'apply')))


if __name__ == '__main__':
    main()
