#!/bin/sh
# Put weightroom and turnstile on the front machine: copy the code, install it, restart the services.
# Run it on compute, from anywhere:
#
#   scripts/deploy-front.sh
#
# The front machine then has a complete copy that runs whether compute is on or off.
# Its own settings, its keys and what it keeps (settings/*.toml, keys/, data/) live only
# there. This script never reads, copies or removes them. What it does copy, beside the code:
# the defaults and the examples of the front machine's programs, and their unit files.
set -eu

FRONT="${FRONT:-10.77.10.42}"   # the front machine, as ssh reaches it
WHERE="${WHERE:-weightroom}"     # the folder there, under the home folder
case "$WHERE" in *[!a-zA-Z0-9_/-]*|/*|*..*) echo 'WHERE must be a simple relative folder'; exit 2;; esac

cd "$(dirname "$0")/.."
# Only committed code is deployed: anything uncommitted, even a new file, stops it here.
if [ -n "$(git status --porcelain)" ]; then
  echo 'The checkout has uncommitted changes. Commit or stash them, then deploy again:'; git status --short; exit 1
fi

# Retire only the old import shim, after installing canonical package metadata.
# Private files or unexpected contents make cleanup fail without deleting the folder.
prune_frontdesk() {
  ssh "$FRONT" "$WHERE/.venv/bin/python - $WHERE" < scripts/prune-frontdesk.py
}

# WeightRoom and its benchmark worker can be updated without restarting turnstile or voice.
# This mode requires an installed weightroom and refreshes its benchmark dependencies.
if [ "${FRONT_ONLY:-0}" = 1 ]; then
  echo "Updating weightroom and benchmarks on $FRONT:~/$WHERE"
  ssh "$FRONT" "test -x $WHERE/.venv/bin/python"
  rsync -a --delete --exclude __pycache__ --exclude LoadCoach-Voice.zip weightroom benchmarks loadcoach engines mirrorwall soundroom db "$FRONT:$WHERE/"
  rsync -a --exclude __pycache__ --exclude settings.json --exclude .settings.lock plugins "$FRONT:$WHERE/"
  rsync -a pyproject.toml "$FRONT:$WHERE/"
  ssh "$FRONT" "set -e
    cd $WHERE
    .venv/bin/python -m pip install -q --upgrade 'pip>=26.2.1'
    .venv/bin/python -m pip install -q -e '.[benchmarks,postgresql,knowledge]'
    .venv/bin/python -m compileall -q weightroom benchmarks plugins engines mirrorwall soundroom db
    systemctl --user restart weightroom.service
    sleep 1
    systemctl --user is-active --quiet weightroom.service"
  prune_frontdesk
  exit 0
fi

# Build the native-client download from an explicit source list, never from local settings.
python3 scripts/package-voice-desktop.py weightroom/static/LoadCoach-Voice.zip

echo "Copying the code to $FRONT:~/$WHERE"
ssh "$FRONT" "mkdir -p $WHERE"
# weightroom itself, and the other packages this repository installs together. Of loadcoach it
# uses one function, the one that checks a password. --delete removes, inside these folders
# only, files that are no longer in the repository.
rsync -a --delete --exclude __pycache__ weightroom turnstile loadcoach benchmarks toollocker spotter voice engines mirrorwall soundroom db "$FRONT:$WHERE/"
# Never delete remote-only plugins or their instance settings; code and state share folders.
rsync -a --exclude __pycache__ --exclude settings.json --exclude .settings.lock plugins "$FRONT:$WHERE/"
rsync -a pyproject.toml "$FRONT:$WHERE/"
ssh "$FRONT" "mkdir -p $WHERE/settings/defaults $WHERE/settings/examples $WHERE/services $WHERE/data $WHERE/keys && chmod 700 $WHERE/keys"
rsync -a settings/defaults/loadcoach.toml settings/defaults/weightroom.toml settings/defaults/voice-gateway.toml settings/defaults/toollocker.toml "$FRONT:$WHERE/settings/defaults/"
rsync -a settings/examples/loadcoach.toml settings/examples/weightroom.toml settings/examples/voice-gateway.toml settings/examples/toollocker.toml "$FRONT:$WHERE/settings/examples/"
rsync -a services/weightroom.service services/turnstile.service services/voice-gateway.service "$FRONT:$WHERE/services/"

echo "Installing"
ssh "$FRONT" "set -e
  cd $WHERE
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade 'pip>=26.2.1'
  .venv/bin/python -m pip install -q -e '.[voice-gateway,benchmarks,postgresql,knowledge]'
  .venv/bin/python -m compileall -q weightroom benchmarks turnstile loadcoach voice plugins engines mirrorwall soundroom db
  # Milestone 75 renamed relay to turnstile, with no alias. Carry relay's service, settings and
  # remembered keys over once; on a front that has already moved, each step finds nothing to do.
  systemctl --user disable -q --now relay.service 2>/dev/null || true
  rm -rf relay ~/.config/systemd/user/relay.service
  [ ! -f settings/weightroom.toml ] || sed -i 's/^\\[relay\\]/[turnstile]/' settings/weightroom.toml
  [ ! -f settings/voice-gateway.toml ] || sed -i 's/^relay =/turnstile =/; s/^voice_relay =/voice_turnstile =/' settings/voice-gateway.toml
  # Milestone 76 keeps the accounts here and checks every key: what relay remembered, and the copy
  # of password hashes kept for compute being off, are of no further use, and the copy is removed.
  rm -f data/relay-keys.json data/turnstile-keys.json data/users.json
  rm -f data/settings-seen-relay.json
  [ ! -f data/settings.json ] || .venv/bin/python -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); \"relay\" in d and json.dump({**{k: v for k, v in d.items() if k != \"relay\"}, \"turnstile\": d[\"relay\"]}, open(p, \"w\"))' data/settings.json
  mkdir -p ~/.config/systemd/user
  cp services/weightroom.service services/turnstile.service services/voice-gateway.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  if [ -f settings/weightroom.toml ] && [ -f keys/weightroom.key ]; then
    systemctl --user enable -q weightroom.service
    systemctl --user restart weightroom.service
    sleep 1
    systemctl --user is-active --quiet weightroom.service
    echo \"weightroom is \$(systemctl --user is-active weightroom.service)\"
    # Opt-in relocation: keep the installed front unit and its private server settings.
    if [ -f settings/toollocker.toml ] && grep -q '^front *=' settings/toollocker.toml; then
      systemctl --user restart toollocker.service
      sleep 1
      systemctl --user is-active --quiet toollocker.service
    fi
    # turnstile runs only once settings/weightroom.toml has a [turnstile] part: without one, nobody has said it should.
    # Restarting it cuts off whatever is being answered through it at that moment.
    if grep -q '^\\[turnstile\\]' settings/weightroom.toml; then
      systemctl --user enable -q turnstile.service
      systemctl --user restart turnstile.service
      sleep 1
      systemctl --user is-active --quiet turnstile.service
      echo \"turnstile is \$(systemctl --user is-active turnstile.service)\"
    else
      echo 'turnstile is installed, not started: settings/weightroom.toml has no [turnstile] part.'
    fi
    if [ -f settings/voice-gateway.toml ] && [ -f keys/voice-gateway.key ]; then
      systemctl --user enable -q voice-gateway.service
      systemctl --user restart voice-gateway.service
      # A process can briefly be active before configuration fails; check an actual route.
      .venv/bin/python -c 'import time,httpx,tomllib; s=tomllib.load(open(\"settings/voice-gateway.toml\", \"rb\")); url=\"http://127.0.0.1:\"+str(s.get(\"port\",8446))+\"/browser/config\"; time.sleep(2); r=httpx.get(url,timeout=5); r.raise_for_status()'
      echo 'voice-gateway is active'
    fi
  else
    echo 'Installed, not started: settings/weightroom.toml or keys/weightroom.key is not there yet.'
    echo 'See the first-time steps in docs/loadcoach.md, section 17.'
  fi"
prune_frontdesk
