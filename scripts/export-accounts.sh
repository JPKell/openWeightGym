#!/bin/bash
# Copy the control node's accounts out, to seed another installation for testing: groups, users (with their
# password hashes, authenticator secrets and settings), API-key hashes, spending allowances, and
# the key files in keys/ that the suite's programs and machines sign in with.
#
#   scripts/export-accounts.sh OUT.tar [ROOT]     ROOT is the control node's installation, ~/weightroom by default
#
# Bring it back with scripts/import-accounts.sh. Nothing in it is encrypted: anyone holding the
# file can sign in as the suite's programs and attempt the password hashes offline. It is made
# readable by you alone; keep it off shared drives and delete it when the test is done.
# Requests, conversations and every other history stay behind. For a full, encrypted move of a
# host to the same paths, use suite-identity instead (docs/installation.md).
set -euo pipefail
OUT=${1:?usage: scripts/export-accounts.sh OUT.tar [ROOT]}
ROOT=$(cd "${2:-$HOME/weightroom}" && pwd)
[ -f "$ROOT/data/accounts.db" ] || { echo "No accounts at $ROOT/data/accounts.db"; exit 1; }
[ ! -e "$OUT" ] || { echo "$OUT already exists; choose a new name."; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"
python3 - "$ROOT/data/accounts.db" "$work/accounts.json" <<'EOF'
import json, sqlite3, sys
database, out = sys.argv[1:]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")  # one moment, while turnstile goes on
    tables = {}
    for table in ("groups", "users", "keys", "spending_allowances"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            tables[table] = [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
with open(out, "w") as file:
    json.dump(tables, file)
print(", ".join(f"{len(rows)} {table}" for table, rows in tables.items()))
EOF
mkdir "$work/keys"
cp -a "$ROOT/keys/." "$work/keys/" 2>/dev/null || true
echo "$(find "$work/keys" -type f | wc -l) key files"
(umask 077 && tar -cf "$OUT" -C "$work" accounts.json keys)
echo "Wrote $OUT. It holds credentials: keep it private."
