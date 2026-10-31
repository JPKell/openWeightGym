#!/usr/bin/env bash
# Set up a remote tool host: this machine serves every tool in its tool folder over HTTP, as one
# systemd user service, for the ToolLocker on compute to reach. Run it on the machine where the
# files are, from a copy of the suite. Running it again updates the suite's own files and the
# service in place, and keeps the key, the settings and every tool file of your own.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE=$(cd -- "$SCRIPT_DIR/.." && pwd)/toollocker  # what is copied comes from here
PREFIX=${XDG_DATA_HOME:-$HOME/.local/share}/toollocker-host
TOOLS=$PREFIX/tools  # the tool folder: every file in it is read when the service starts
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/toollocker-host
KEY=$CONFIG/host.key
SETTINGS=$CONFIG/tools.toml
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNIT=toollocker-host.service
SYSTEMCTL=${SYSTEMCTL:-systemctl}  # a test puts a stand-in here

NAME=
DESCRIPTION=
ADDRESS=$(hostname)
COMPUTE=
FILES=()
CODE=()
PORT=9100
BIND=
START=1

usage() {
  cat <<'EOF'
Usage: scripts/install-tool-host.sh --name NAME [--files FOLDER]... [--code FOLDER]... [options]

Serves every tool in this machine's tool folder, ~/.local/share/toollocker-host/tools, to the
ToolLocker on compute, as one service on one port. The suite's own `files` and `code` tools are
put there; add tools of your own by putting files beside them (see the README copied beside
the folder). --files and --code are folders for those two, written to the tool settings the
first time. Each option may be given more than once.

  --name NAME          what compute calls this machine: lowercase letters, digits and dashes.
                       Its tools are offered as NAME__read_file, NAME__search, ...
  --description TEXT   what is here, shown to models in front of each tool's own description
  --files FOLDER       a folder the `files` tools may list and read
  --code FOLDER        a git repository, or a folder of them, the `code` tools may read
  --port PORT          the port compute reaches (default 9100)
  --bind ADDRESS       listen only on this IP address (default: all interfaces)
  --address HOST       how compute reaches this machine (default: this machine's hostname)
  --compute ADDRESS    compute's address, to print the firewall rule that admits only it
  --no-start           write everything, but do not start or restart the service
  -h, --help           show this help

The key and the tool settings are kept in ~/.config/toollocker-host. Once the settings exist,
change folders by editing ~/.config/toollocker-host/tools.toml and restarting the service.
EOF
}

fail() { echo "install-tool-host: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --name) NAME=${2:-}; shift 2 ;;
    --description) DESCRIPTION=${2:-}; shift 2 ;;
    --files) FILES+=("${2:-}"); shift 2 ;;
    --code) CODE+=("${2:-}"); shift 2 ;;
    --port) PORT=${2:-}; shift 2 ;;
    --bind) BIND=${2:-}; shift 2 ;;
    --address) ADDRESS=${2:-}; shift 2 ;;
    --compute) COMPUTE=${2:-}; shift 2 ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown option $1" ;;
  esac
done

# ---- check what was asked, before anything is changed ------------------------------------------

[[ $NAME =~ ^[a-z0-9][a-z0-9-]*$ ]] || fail "--name is lowercase letters, digits and dashes, such as nas"
[[ $PORT =~ ^[0-9]+$ ]] && ((PORT > 0 && PORT < 65536)) || fail "a port is a number from 1 to 65535: $PORT"
for folder in "${FILES[@]}" "${CODE[@]}"; do
  [[ -d $folder ]] || fail "not a folder: $folder"
done
if [[ -e $SETTINGS ]] && ((${#FILES[@]} + ${#CODE[@]})); then
  fail "the tool settings exist already. Edit $SETTINGS to change folders, then run this again without --files and --code"
fi
((${#CODE[@]} == 0)) || command -v git >/dev/null || fail "the code tools need git, which is not installed"
[[ -f $SOURCE/tools/__main__.py ]] || fail "run this from a copy of the suite: $SOURCE/tools is missing"
PYTHON=$(command -v python3) || fail "python3 is not installed"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail "python3 must be 3.11 or later"
if [[ -n $BIND ]]; then
  "$PYTHON" -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "$BIND" 2>/dev/null || fail "--bind must be an IP address"
fi

# ---- the program, the tool folder, the key, the settings and the service -----------------------

# The program that reads the tool folder needs nothing beyond Python's own library. The suite's
# own tool files are replaced on every run; any other file in the tool folder is yours and kept.
mkdir -p "$PREFIX/toollocker/tools" "$TOOLS"
chmod 700 "$PREFIX" "$PREFIX/toollocker" "$PREFIX/toollocker/tools" "$TOOLS"
cp "$SOURCE/__init__.py" "$PREFIX/toollocker/"
cp "$SOURCE/tools/__init__.py" "$SOURCE/tools/__main__.py" "$PREFIX/toollocker/tools/"
cp "$SOURCE/tools/files.py" "$SOURCE/tools/code.py" "$TOOLS/"
cp "$SOURCE/README.md" "$PREFIX/README.md"
chmod 600 "$PREFIX/toollocker/__init__.py" "$PREFIX/toollocker/tools/"*.py "$TOOLS/"*.py "$PREFIX/README.md"

mkdir -p "$CONFIG"
chmod 700 "$CONFIG"
if [[ ! -s $KEY ]]; then
  (umask 077 && "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$KEY")
fi
chmod 600 "$KEY"

# The tool settings, written once: a [NAME] section for each tool file NAME.py that needs one.
# A folder not given is written as a comment, ready to fill in.
if [[ ! -e $SETTINGS ]]; then
  "$PYTHON" - "$SETTINGS" "${#FILES[@]}" "${FILES[@]}" "${CODE[@]}" <<'PY'
import json, sys
from pathlib import Path
path, count, folders = sys.argv[1], int(sys.argv[2]), [str(Path(one).resolve()) for one in sys.argv[3:]]
lines = ["# Settings for the tools in ~/.local/share/toollocker-host/tools: one section for each",
         "# tool file NAME.py that has a setup function. Without its section, that file is left out.",
         "# After a change: systemctl --user restart toollocker-host.service", ""]
for name, key, chosen in (("files", "folders", folders[:count]), ("code", "repositories", folders[count:])):
    section = [f"[{name}]", f"{key} = [{', '.join(json.dumps(one) for one in chosen)}]", ""]
    lines += section if chosen else [f"# {line}" if line else line for line in section]
Path(path).write_text("\n".join(lines))
PY
fi
chmod 600 "$SETTINGS"

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=ToolLocker tool host for compute ($NAME)
After=network-online.target

[Service]
WorkingDirectory=$PREFIX
ExecStart=$PYTHON -m toollocker.tools "$TOOLS" --settings "$SETTINGS" --http $PORT --key-file "$KEY" ${BIND:+--bind $BIND}
NoNewPrivileges=true
UMask=0077
MemoryMax=512M
TasksMax=32
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
chmod 600 "$UNIT_DIR/$UNIT"

"$SYSTEMCTL" --user daemon-reload
if ((START)); then
  "$SYSTEMCTL" --user enable "$UNIT"
  "$SYSTEMCTL" --user restart "$UNIT"
fi

# ---- what to do on compute ---------------------------------------------------------------------

description=
[[ -z $DESCRIPTION ]] || description="description = $("$PYTHON" -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$DESCRIPTION")"$'\n'

cat <<EOF

Set up on this machine: $UNIT, serving every tool in $TOOLS
$( ((START)) && echo "Started. Check with: journalctl --user -u $UNIT, which lists the tools it found." || echo "Not started (--no-start)." )
Tool settings: $SETTINGS. How to add tools of your own: $PREFIX/README.md
To keep it running while nobody is signed in here: sudo loginctl enable-linger $USER

On compute:

1. Put this key in ~/ai/suite/keys/$NAME.key, readable by its owner only (chmod 600):

$(cat "$KEY")

2. Add to ~/ai/suite/settings/toollocker.toml, choosing its level:

[servers.$NAME]
url = "http://$ADDRESS:$PORT/mcp"
key_file = "../keys/$NAME.key"
${description}level = "normal"

3. Restart the ToolLocker: systemctl --user restart toollocker.service

Let only compute reach port $PORT. For example, with ufw on this machine:
  sudo ufw allow from ${COMPUTE:-COMPUTE_ADDRESS} to any port $PORT proto tcp
or a Tailscale ACL that admits only compute to that port. The key travels unencrypted over plain HTTP.
EOF
