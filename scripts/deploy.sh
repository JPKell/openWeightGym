#!/bin/bash
# Deploy and update everything: front's code and services, then compute's, with any one-time moves of
# data to front that the new code needs. Run it on compute, from the checkout compute's services run from:
#
#   scripts/deploy.sh          says what it will do, and asks before doing it
#   scripts/deploy.sh --yes    does it without asking
#
# In order:
#   1. puts the code on front and restarts weightroom, turnstile and voice-gateway there (scripts/deploy-front.sh)
#   2. takes a copy of compute's database, at one moment, while LoadCoach goes on
#   3. with turnstile and weightroom stopped on front, moves what front does not have yet, once each:
#        weightroom roles-from   the roles and recent reliability (milestone 83)
#        weightroom log-from     the event log and the requests kept in full (milestone 91)
#        weightroom outside-from the outside services (milestone 114); their NAME.key files are copied across first,
#                                 into front's keys folder, only where front has none, readable by you alone
#        weightroom batches-from the uploaded files and batches, those under way going on there (milestone 115)
#      A move that was made before is skipped. DB1-retired stores skip all legacy moves.
#   4. restarts compute's programs that are running, so that they take the new code
#   5. checks that a key works through turnstile and is refused by compute itself, and that compute's
#      log reaches front
#
# From step 1 until step 4 is done, a minute or two, front runs new code and compute old: role requests
# and the log pages may fail. Restarting LoadCoach ends what it is answering and unloads its models.
# Compute's own copies are left as they were; they are the way back. Entries compute writes after the
# copy is taken are not lost: compute sends front everything it has once it restarts, and front skips
# what it has.
set -eu

FRONT="${FRONT:-10.77.10.42}"     # the front machine, as ssh reaches it
WHERE="${WHERE:-weightroom}"       # its folder there, under the home folder
case "$WHERE" in *[!a-zA-Z0-9_/-]*|/*|*..*) echo 'WHERE must be a simple relative folder'; exit 2;; esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
COMPUTE="$(.venv/bin/python -c 'import tomllib; print(tomllib.load(open("settings/loadcoach.toml", "rb")).get("host", "127.0.0.1"))')"  # where LoadCoach listens
COPY="/tmp/compute-data-$$.db"
REMOTE="/tmp/compute-data.db"
trap 'rm -f "$COPY"' EXIT

say() { printf '\n== %s\n' "$*"; }

systemctl --user show loadcoach -p ExecStart | grep -q "$ROOT/.venv/bin/loadcoach" ||
  { echo "loadcoach.service does not run from $ROOT. Run this from the checkout it runs from."; exit 1; }
ssh -o BatchMode=yes -o ConnectTimeout=5 "$FRONT" "test -d $WHERE/settings" || { echo "Cannot reach $FRONT:~/$WHERE."; exit 1; }

echo "Deploy this checkout ($(git log --oneline -1)) to front and compute. The steps are at the top of $0."
if [ "${1:-}" != "--yes" ]; then
  read -r -p "Go ahead? [y/N] " answer
  [ "$answer" = y ] || [ "$answer" = Y ] || { echo "Nothing done."; exit 0; }
fi

say "1. The code on front"
scripts/deploy-front.sh

say "2. A copy of compute's database"
.venv/bin/python - settings/loadcoach.toml "$COPY" <<'EOF'
import sqlite3, sys
from pathlib import Path
from db import local_path
from loadcoach.config import kept_by_loadcoach

source = local_path(kept_by_loadcoach(Path(sys.argv[1]))[1])
if source is None:
    raise SystemExit("LoadCoach's database is not a SQLite file here: move the roles and the log by hand.")
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old, sqlite3.connect(sys.argv[2]) as copy:
    old.backup(copy)  # the whole file as it is at one moment, while LoadCoach goes on writing
Path(sys.argv[2]).chmod(0o600)
print(f"Copied {source}.")
EOF
scp -q "$COPY" "$FRONT:$REMOTE"

say "3. What front does not have yet"
RETIRED="$(.venv/bin/python - "$COPY" <<'EOF'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as connection:
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='suite_schema_migrations'").fetchone()
    print(int(bool(exists and connection.execute(
        "SELECT 1 FROM suite_schema_migrations WHERE stream='loadcoach.retirement' AND version>=1").fetchone())))
EOF
)"
if [ "$RETIRED" = 1 ]; then
  echo "DB1 retirement verified: front owns this data; legacy copy commands are skipped."
  ssh "$FRONT" "rm -f $REMOTE"
else
# An outside service's key is a file, never in a database. Copy each service's across, once: front is where it is used now.
for service in $(python3 -c "
import sqlite3, sys
try:
    print(*[name for (name,) in sqlite3.connect('file:$COPY?mode=ro', uri=True).execute('SELECT name FROM services')])
except sqlite3.Error:
    pass
"); do
  if [ -f "keys/$service.key" ] && ! ssh "$FRONT" "test -f $WHERE/keys/$service.key"; then
    scp -q "keys/$service.key" "$FRONT:$WHERE/keys/$service.key" && ssh "$FRONT" "chmod 600 $WHERE/keys/$service.key" && echo "key of $service copied to front"
  fi
done
ssh "$FRONT" "set -u
  cd $WHERE
  chmod 600 $REMOTE
  systemctl --user stop turnstile.service weightroom.service
  failed=0
  for move in roles-from log-from outside-from batches-from; do
    if said=\$(.venv/bin/weightroom --config settings/weightroom.toml \$move $REMOTE 2>&1); then
      echo \"\$said\"
    elif echo \"\$said\" | grep -q 'first move only'; then
      echo \"\$move: done already\"
    else
      echo \"\$move failed: \$said\"; failed=1
    fi
  done
  rm -f $REMOTE
  systemctl --user start turnstile.service weightroom.service
  exit \$failed"
fi

say "4. Compute's programs, started again"
for unit in loadcoach toollocker spotter voice; do
  if systemctl --user is-active --quiet "$unit.service"; then
    systemctl --user restart "$unit.service" && echo "$unit restarted"
  else
    echo "$unit is not running: it takes the new code when it is next started"
  fi
done

say "5. Checks"
sleep 8
key="\$(cat $WHERE/keys/weightroom.key)"
through=$(ssh "$FRONT" "curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer $key\" http://127.0.0.1:8442/v1/models")
around=$(ssh "$FRONT" "curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer $key\" http://$COMPUTE:8442/v1/models")
echo "weightroom's key through turnstile: $through (200 is right)"
echo "the same key straight to compute:   $around (401 is right)"
echo "The latest of the log on front, which should include compute's 'loadcoach started':"
ssh "$FRONT" "cd $WHERE && .venv/bin/weightroom --config settings/weightroom.toml log -n 5"
[ "$through" = 200 ] && [ "$around" = 401 ] && echo "Done." || { echo "A check did not come out right: look before going on."; exit 1; }
