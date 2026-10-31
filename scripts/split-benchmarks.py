"""One-time, offline move of benchmark evidence to the standalone service database.

Stop LoadCoach and the benchmark service first. A private SQLite backup is made before any
source change. Historical benchmark tables remain untouched in the source as a rollback copy;
only their owning batches are handed to the generic external-task API. Never overwrites a
nonempty destination. The live benchmark application never reads the source database.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3

from benchmarks.store import Benchmarks
from loadcoach.tasks import ENDPOINT, SCHEMA


def migrate(source: Path, destination: Path, backup: Path):
    """Copy and verify every sample before assigning scoped external-task credentials."""
    if source.resolve() == destination.resolve():
        raise ValueError('The benchmark database must be separate from LoadCoach.')
    if destination.exists():
        raise ValueError('Destination already exists; refusing to overwrite benchmark evidence.')
    if backup.exists():
        raise ValueError('Backup already exists; choose a new backup path.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    backup.parent.mkdir(parents=True,exist_ok=True)
    source_db = sqlite3.connect(f'file:{source.resolve()}?mode=rw',uri=True)
    old = sqlite3.connect(backup)
    os.chmod(backup,0o600)
    source_db.backup(old)
    old.close()
    store = Benchmarks(str(destination))
    try:
        rows = source_db.execute('SELECT r.*,b.user,b.status FROM benchmark_runs r JOIN batches b ON b.id=r.id').fetchall()
        columns = [row[1] for row in source_db.execute('PRAGMA table_info(benchmark_runs)')]
        count = source_db.execute('SELECT count(*) FROM benchmark_runs').fetchone()[0]
        if count != len(rows):
            raise ValueError('A benchmark run has no owning batch; repair it before migrating.')
        tokens = {}
        with store.db:
            for row in rows:
                values = dict(zip(columns,row[:-2]))
                values.update(user=row[-2],token=secrets.token_urlsafe(32),synced=int(row[-1] not in ('in_progress','cancelling')))
                if values['state']=='running':
                    values['state']='queued'
                names = list(values)
                store.db.execute(f'INSERT INTO benchmark_runs({",".join(names)}) VALUES({",".join("?" for _ in names)})',list(values.values()))
                tokens[values['id']] = values['token']
            samples = source_db.execute('SELECT * FROM benchmark_samples ORDER BY run,sample').fetchall()
            store.db.executemany('INSERT INTO benchmark_samples VALUES(?,?,?,?,?,?)',samples)
            store.db.executemany('INSERT OR REPLACE INTO benchmark_definitions(id,enabled,formula) VALUES(?,?,?)',source_db.execute('SELECT id,enabled,formula FROM benchmark_definitions').fetchall())
            paused = source_db.execute('SELECT paused FROM benchmark_state WHERE id=1').fetchone()[0]
            store.db.execute('UPDATE benchmark_state SET paused=? WHERE id=1',(paused,))
        assert store.db.execute('SELECT * FROM benchmark_samples ORDER BY run,sample').fetchall()==samples
        assert store.db.execute('SELECT count(*) FROM benchmark_runs').fetchone()[0]==count
        source_db.executescript(SCHEMA)
        with source_db:
            for row in rows:
                values = dict(zip(columns,row[:-2]))
                number = values['id']
                source_db.execute('INSERT INTO external_tasks VALUES(?,?,?,?,?,?)',
                    (number,hashlib.sha256(tokens[number].encode()).hexdigest(),values['model'],values['fingerprint'],values['subject'],values['environment']))
                source_db.execute('UPDATE batches SET endpoint=? WHERE id=?',(ENDPOINT,number))
        return {'runs':count,'samples':len(samples),'backup':str(backup)}
    finally:
        store.db.close()
        source_db.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--backup',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(migrate(args.source,args.destination,args.backup)))
