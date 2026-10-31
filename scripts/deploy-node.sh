#!/bin/bash
# Put the code of this checkout on one machine and restart its services. WeightRoom's Deploy page
# runs it on front, from front's source checkout; it can also be run by hand:
#
#   scripts/deploy-node.sh HOST FOLDER EXTRAS [SERVICE...]   copy, install, restart
#   scripts/deploy-node.sh --restart HOST [SERVICE...]       restart only
#   scripts/deploy-node.sh --setup HOST FOLDER SERVICE,...   first install on a fresh machine
#
# --setup refuses a folder that exists or a service whose unit is already installed there. It
# copies the code and runs scripts/install-suite.sh, which makes the .venv, renders settings from
# the examples and installs and enables the units. New settings are left for review, so their
# services are not started: edit them on the machine, then restart from the Deploy page.
#
# HOST is how ssh reaches the machine, or "local" for this one. FOLDER is under the home folder
# there. EXTRAS are pyproject extras, comma separated, or "-" for none. Only code is copied:
# settings/*.toml, keys/, data/ and .env are never read, copied or removed. Services restart in
# the order given, so the one running this (weightroom on front) goes last.
set -euo pipefail
cd "$(dirname "$0")/.."

mode=deploy
case "${1:-}" in --restart) mode=restart; shift;; --setup) mode=setup; shift;; esac
HOST=${1:?HOST}; shift
case "$HOST" in -*|*[!a-zA-Z0-9_.@:-]*) echo "HOST must be a plain ssh destination"; exit 2;; esac

on() {
  if [ "$HOST" = local ]; then (cd ~ && bash -c "$1"); else ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$1"; fi
}

failed=0
restart() {
  for unit in "$@"; do
    case "$unit" in *[!a-z0-9_-]*) echo "skipping $unit: not a service name"; failed=1; continue;; esac
    if on "systemctl --user restart $unit.service && sleep 2 && systemctl --user is-active --quiet $unit.service"; then
      echo "$unit restarted"
    else
      echo "$unit did not come back up"; failed=1
    fi
  done
}

if [ $mode = restart ]; then
  restart "$@"
  exit $failed
fi

FOLDER=${1:?FOLDER} EXTRAS=${2:?EXTRAS}; shift 2
case "$FOLDER" in -*|*[!a-zA-Z0-9_/-]*|/*|*..*) echo 'FOLDER must be a simple relative folder'; exit 2;; esac
case "$EXTRAS" in -) EXTRAS="";; *[!a-z,-]*) echo 'EXTRAS must be names separated by commas'; exit 2;; esac
# Only committed code is deployed: anything uncommitted, even a new file, stops it here.
if [ -n "$(git status --porcelain)" ]; then
  echo 'The checkout has uncommitted changes. Commit or stash them, then deploy again:'; git status --short; exit 1
fi
commit=$(git rev-parse HEAD)

if [ $mode = setup ]; then
  # EXTRAS is the service list here: install-suite.sh picks the extras for them itself.
  echo "Setting up $HOST:~/$FOLDER with $EXTRAS from $(git log -1 --format='%h %s')"
  on "set -e
    test ! -e $FOLDER || { echo '~/$FOLDER already exists: setup is only for a fresh machine. Use Deploy.'; exit 1; }
    for unit in ${EXTRAS//,/ }; do
      test ! -e ~/.config/systemd/user/\$unit.service || { echo \"\$unit is already installed there. Use Deploy.\"; exit 1; }
    done
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else \"Python 3.13 or newer is needed; this machine has \" + sys.version.split()[0])'
    command -v rsync >/dev/null || { echo 'rsync is not installed there.'; exit 1; }
    systemctl --user show-environment >/dev/null || { echo 'systemd --user is not available there.'; exit 1; }
    [ \"\$(loginctl show-user \"\$USER\" -p Linger --value 2>/dev/null)\" = yes ] || echo 'Note: lingering is off, so the services stop when you log out there. Turn it on with: sudo loginctl enable-linger \$USER'"
fi

echo "Deploying $(git log -1 --format='%h %s') to $HOST:~/$FOLDER"
if [ "$HOST" = local ]; then to="$HOME/$FOLDER"; else to="$HOST:$FOLDER"; fi
python3 scripts/package-voice-desktop.py weightroom/static/LoadCoach-Voice.zip
on "mkdir -p $FOLDER/settings"
# --delete removes, inside these package folders only, files no longer in the repository.
rsync -a --delete --exclude __pycache__ weightroom turnstile loadcoach benchmarks toollocker spotter voice engines mirrorwall soundroom db "$to/"
# Plugins share folders with their instance settings, and a machine may keep scripts or units of its own.
rsync -a --exclude __pycache__ --exclude settings.json --exclude .settings.lock plugins scripts services pyproject.toml "$to/"
rsync -a settings/defaults settings/examples "$to/settings/"

if [ $mode = setup ]; then
  echo "Installing with scripts/install-suite.sh"
  on "set -e
    cd $FOLDER
    scripts/install-suite.sh --services $EXTRAS --database sqlite --non-interactive --yes
    echo $commit > DEPLOYED"
  echo "Set up $commit. Review the new settings in ~/$FOLDER/settings on $HOST (front's address, the machine key"
  echo "and machine_name), add the machine to [machines] on front, then use Restart services."
  exit 0
fi

echo "Installing"
on "set -e
  cd $FOLDER
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade 'pip>=26.2.1'
  .venv/bin/python -m pip install -q -e '.${EXTRAS:+[$EXTRAS]}'
  .venv/bin/python -m compileall -q engines mirrorwall soundroom db loadcoach toollocker spotter weightroom turnstile voice benchmarks plugins
  echo $commit > DEPLOYED"

restart "$@"
[ $failed = 0 ] && echo "Deployed $commit." || echo "Deployed $commit, but a service did not come back up."
exit $failed
