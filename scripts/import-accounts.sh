#!/bin/bash
# Load accounts written by scripts/export-accounts.sh into another installation, for testing it
# with the users, passwords and API keys you already have.
#
#   scripts/import-accounts.sh IN.tar [ROOT]       ROOT is the new front installation, ~/weightroom by default
#
# Run it from that installation's folder (its .venv makes the accounts tables if they are not
# there yet). Stop turnstile and weightroom there first. Groups, users, key hashes and allowances
# with the same name are replaced by the exported ones; others there are kept. Key files are
# copied into ROOT/keys only where ROOT has none of that name, so a key the new installation made
# for itself is kept, and named, so you can choose.
set -euo pipefail
IN=$(realpath "${1:?usage: scripts/import-accounts.sh IN.tar [ROOT]}")
ROOT=$(cd "${2:-$HOME/weightroom}" && pwd)
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "$ROOT has no .venv: install it first (scripts/install-suite.sh)."; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"
tar -xf "$IN" -C "$work"
mkdir -p "$ROOT/data" "$ROOT/keys"
chmod 700 "$ROOT/keys"

(cd "$ROOT" && "$PYTHON" - "$ROOT/data/accounts.db" "$work/accounts.json") <<'EOF'
import json, sys
from loadcoach.users import Users

database, exported = sys.argv[1:]
Users(database, {})  # makes the tables, at this installation's schema, if they are not there
import sqlite3
tables = json.load(open(exported))
with sqlite3.connect(database) as db:
    for table, rows in tables.items():
        have = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        for row in rows:
            columns = [name for name in row if name in have]  # a column this schema lacks is left out
            db.execute(f'INSERT OR REPLACE INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in columns)}) '
                       f'VALUES ({", ".join("?" for _ in columns)})', [row[name] for name in columns])
        print(f"{len(rows)} {table}")
EOF

for file in "$work"/keys/*; do
  [ -e "$file" ] || continue
  name=$(basename "$file")
  if [ -e "$ROOT/keys/$name" ]; then
    cmp -s "$file" "$ROOT/keys/$name" || echo "kept $ROOT/keys/$name, which differs from the exported one"
  else
    cp -a "$file" "$ROOT/keys/$name" && echo "key $name copied"
  fi
done
echo "Imported. Start turnstile and weightroom there and sign in as before."
