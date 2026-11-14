#!/usr/bin/env bash
# Install selected openWeightGym programs on this machine and configure their database stores.
# Existing units, settings, and databases are discovered and checked before changes are made.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib/ui.sh
source "$SCRIPT_DIR/lib/ui.sh"
# shellcheck source=lib/detect.sh
source "$SCRIPT_DIR/lib/detect.sh"
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
SERVICES=(loadcoach toollocker spotter voice weightroom turnstile voice-gateway)
COMPUTE=(loadcoach spotter voice)
FRONT=(weightroom turnstile toollocker voice-gateway)

CHECK_ONLY=0
DRY_RUN=0
NON_INTERACTIVE=0
START_SERVICES=1
ASSUME_YES=0
DATABASE=auto
SERVICE_ARGUMENT=
PRESET=
INSTALL_POSTGRESQL=0
PROVISION_POSTGRESQL=0
INSTALL_PGVECTOR=0
SDCPP_ACTION=keep
SDCPP_BACKEND=cuda
SDCPP_EXPLICIT=0
AUDIOCPP_ACTION=keep
WHISPERCPP_ACTION=keep
AUDIOCPP_BACKEND=cuda
WHISPERCPP_BACKEND=cuda
AUDIOCPP_EXPLICIT=0
WHISPERCPP_EXPLICIT=0
STRATA_ACTION=keep
STRATA_EXPLICIT=0
STRATA_HOST_RAM_MIB=22528
CLASSIFIER_TOOLS_ACTION=keep
CLASSIFIER_TOOLS_EXPLICIT=0
TOPOLOGY=keep
ADDRESS=
FRONT_URL=
COMPUTE_URL=
LLAMA_SERVER=
MACHINE_NAME=
VOICE_MODEL=
PLATFORM_SERVICES=images,speech,audio,downloads,classifiers
START_NEW=0
KEEP_EXISTING_UNITS=0
UV=
EMBEDDED=0
declare -A DSNS=()
declare -A WAS_CONFIGURED=()
declare -A CONFIG_BACKUPS=()
declare -A EXISTING_INSTALL_SERVICES=()
PG_CONNECTIONS_CHECKED=0

usage() {
  cat <<'EOF'
Usage: scripts/install-suite.sh [options]

Interactive use discovers installed openWeightGym units, preselects them, and lets you
change both the service set and database engine before anything is installed.

  --check                    audit this installation and make no changes
  --dry-run                  show the actions without changing the machine
  --services LIST            comma-separated names, or compute, front, all
  --preset compute|front|all fresh-install service preset
  --database keep|sqlite|postgresql
  --dsn NAME=FILE            PostgreSQL DSN file (core, telemetry, accounts, roles, chat,
                             benchmarks, plugins, alerts, or schedules); repeat as needed
  --install-postgresql       install PostgreSQL server and client packages
  --provision-postgresql     create local openWeightGym roles/databases and DSN files
  --pgvector                 install the pgvector package (implies PostgreSQL)
  --strata install|skip|keep  optional Strata CUDA runtime (default: keep)
  --strata-host-ram-mib N     explicit host RAM budget (default: 22528 MiB)
  --sdcpp install|skip|keep   optional image runtime (fresh unattended: skip)
  --sdcpp-backend cuda|vulkan|cpu
                             explicit native backend; default cuda, no CPU fallback
  --audiocpp install|skip|keep  optional audio runtime (fresh unattended: skip)
  --whispercpp install|skip|keep  optional Whisper runtime (fresh unattended: skip)
  --audiocpp-backend cuda|vulkan|cpu
  --whispercpp-backend cuda|vulkan|cpu
                             explicit native backend; default cuda, no CPU fallback
  --classifier-tools install|skip|keep
                             optional CPU classifier conversion tools (default: keep)
  --no-start                 install and enable units without starting them
  --topology all|front|compute
                             write new settings for this machine's part: everything on
                             one machine, the console only, or a GPU node (default: keep
                             the examples' placeholders for you to edit)
  --address IP               this machine's address on its network (GPU node settings)
  --front-url URL            the console's turnstile, such as http://192.168.1.10:8442
  --compute-url URL          the GPU node's LoadCoach, such as http://192.168.1.20:8442
  --llama-server PATH        the llama-server LoadCoach runs (see install-llamacpp.sh)
  --machine-name NAME        the name the console knows this GPU node by (default: compute)
  --voice-model NAME         conversation model for newly written voice settings (default: the voice defaults')
  --platform-services LIST   initial console services: images,speech,audio,downloads,classifiers or none
  --start-new                also start services whose settings were just written
  --keep-existing-units      preserve existing unit customizations when adding components
  --uv PATH                  make the environment and install packages with uv
  --embedded                 leave headings and the summary to the program calling this
  --non-interactive          require all choices on the command line
  --yes                      accept the final interactive summary
  --root PATH                install from a different openWeightGym source checkout
  -h, --help                 show this help

Examples:
  scripts/install-suite.sh
  scripts/install-suite.sh --check --services compute
  scripts/install-suite.sh --services front --database sqlite --yes
  scripts/install-suite.sh --services all --database postgresql \
    --install-postgresql --provision-postgresql --pgvector --yes
EOF
}

# Stop, saying what went wrong and what to do about it: die "What went wrong" ["What to do" ...]
die() { ui_die "$@"; }
# A mistake in the options given: usage_error "What is wrong"
usage_error() { die "$1" "See what each option takes: $(shell_path "$SCRIPT_DIR/install-suite.sh") --help"; }
note() { printf '%s\n' "$*"; }
quote_command() { printf ' %q' "$@"; printf '\n'; }
# A short command whose output is shown and kept in the log. It reads nothing: under
# `curl | bash` its input would be the rest of the installer.
run() {
  if ((DRY_RUN)); then
    printf '  %sDRY RUN:%s' "$C_MUTED" "$C_RESET"
    quote_command "$@"
    return 0
  fi
  ui_log_mark
  ui__log "" "==== $1 ${2:-}"
  "$@" </dev/null 2>&1 | tee -a "${OWG_LOG:-/dev/null}" && return 0
  local rc=$?
  UI_FAILED_MARK=$UI_LOG_MARK
  return "$rc"
}
# A longer piece of work, with a spinner and its output in the log; its caller says what to do
# when it fails: try_step LABEL COMMAND... || die ...
try_step() {
  local label=$1
  shift
  if ((DRY_RUN)); then run "$@"; return 0; fi
  ui_step "$label" -- "$@"
}
# Moving an environment aside, so that the next run makes a new one: what to do when it is unusable.
venv_aside() { printf 'Move it aside, and the next run makes a new one: mv %s %s' "$(shell_path "$ROOT/.venv")" "$(shell_path "$ROOT/.venv.old")"; }

# How to carry on, for the steps a failure lists: the installer that called this, or this script.
: "${OWG_RETRY_COMMAND:=$0}"
: "${OWG_STOP_TITLE:=Setting up stopped}"
export OWG_STOP_TITLE

# Anything that fails without a message of its own still stops loudly, with where it happened.
on_error() {  # on_error STATUS FILE LINE COMMAND, from the ERR trap
  local status=$1 file=${2##*/} line=$3 command=$4
  ((BASH_SUBSHELL == 0)) || return 0
  trap - ERR
  ui_die "Setting up stopped unexpectedly, at ${file:-install-suite.sh} line $line (exit status $status), running:
  $command" \
    "The lines just above it may say why: put that right" \
    "If it happens again, keep the log for whoever looks into it"
}

repair_postgresql() {
  ((PROVISION_POSTGRESQL && !DRY_RUN && !CHECK_ONLY && !PG_CONNECTIONS_CHECKED)) || return 0
  if compgen -G "$ROOT/keys/postgresql/*.dsn" >/dev/null; then
    try_step 'Recovering retained PostgreSQL connections' "$PY" "$ROOT/scripts/install-suite-support.py" repair-postgresql --root "$ROOT" ||
      die 'The PostgreSQL connections kept from an earlier installation could not be recovered.' \
        "The lines above, from the log, say why: put that right" "?Check PostgreSQL answers: sudo -u postgres psql -c 'SELECT 1'" "${PG_INSTEAD[@]}"
  fi
  PG_CONNECTIONS_CHECKED=1
}
section() { ((EMBEDDED)) || ui_section "$@"; }
# The compatibility audit, its findings shown like the rest: audit [--quiet] PYTHON ARGUMENTS...
# Quiet shows only what is wrong.
audit() {
  local quiet=0 python output rc=0 line level rest
  [[ $1 == --quiet ]] && { quiet=1; shift; }
  python=$1
  shift
  output=$("$python" "$ROOT/scripts/install-suite-support.py" audit "$@" 2>&1) || rc=$?
  while IFS= read -r line; do
    level=${line%% *} rest=${line#"$level"}
    rest=${rest#"${rest%%[! ]*}"}
    case "$level" in
      OK|NEW|Compatibility:) ((quiet)) && continue ;;&
      OK) ui_ok "$rest" ;;
      NEW) ui_info "$rest" ;;
      WARN) ui_warn "$rest" ;;
      ERROR) ui_fail "$rest" ;;
      *) [[ -n $line ]] && ui_note "$line" ;;
    esac
  done <<<"$output"
  return "$rc"
}

while (($#)); do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --services) (($# >= 2)) || usage_error '--services needs a value'; SERVICE_ARGUMENT=$2; shift 2 ;;
    --preset) (($# >= 2)) || usage_error '--preset needs a value'; PRESET=$2; shift 2 ;;
    --database) (($# >= 2)) || usage_error '--database needs a value'; DATABASE=$2; shift 2 ;;
    --dsn)
      (($# >= 2)) || usage_error '--dsn needs NAME=FILE'
      [[ $2 == *=* ]] || usage_error '--dsn needs NAME=FILE'
      DSNS[${2%%=*}]=${2#*=}; shift 2 ;;
    --install-postgresql) INSTALL_POSTGRESQL=1; shift ;;
    --provision-postgresql) PROVISION_POSTGRESQL=1; INSTALL_POSTGRESQL=1; shift ;;
    --pgvector) INSTALL_PGVECTOR=1; INSTALL_POSTGRESQL=1; shift ;;
    --strata) (($# >= 2)) || usage_error '--strata needs install, skip or keep'; STRATA_ACTION=$2; STRATA_EXPLICIT=1; shift 2 ;;
    --strata-host-ram-mib) (($# >= 2)) || usage_error '--strata-host-ram-mib needs a positive MiB budget'; STRATA_HOST_RAM_MIB=$2; shift 2 ;;
    --sdcpp) (($# >= 2)) || usage_error '--sdcpp needs install, skip or keep'; SDCPP_ACTION=$2; SDCPP_EXPLICIT=1; shift 2 ;;
    --sdcpp-backend) (($# >= 2)) || usage_error '--sdcpp-backend needs cuda, vulkan or cpu'; SDCPP_BACKEND=$2; shift 2 ;;
    --audiocpp) (($# >= 2)) || usage_error '--audiocpp needs install, skip or keep'; AUDIOCPP_ACTION=$2; AUDIOCPP_EXPLICIT=1; shift 2 ;;
    --whispercpp) (($# >= 2)) || usage_error '--whispercpp needs install, skip or keep'; WHISPERCPP_ACTION=$2; WHISPERCPP_EXPLICIT=1; shift 2 ;;
    --audiocpp-backend) (($# >= 2)) || usage_error '--audiocpp-backend needs cuda, vulkan or cpu'; AUDIOCPP_BACKEND=$2; shift 2 ;;
    --whispercpp-backend) (($# >= 2)) || usage_error '--whispercpp-backend needs cuda, vulkan or cpu'; WHISPERCPP_BACKEND=$2; shift 2 ;;
    --classifier-tools) (($# >= 2)) || usage_error '--classifier-tools needs install, skip or keep'; CLASSIFIER_TOOLS_ACTION=$2; CLASSIFIER_TOOLS_EXPLICIT=1; shift 2 ;;
    --no-start) START_SERVICES=0; shift ;;
    --topology) (($# >= 2)) || usage_error '--topology needs all, front or compute'; TOPOLOGY=$2; shift 2 ;;
    --address) (($# >= 2)) || usage_error '--address needs an IP address'; ADDRESS=$2; shift 2 ;;
    --front-url) (($# >= 2)) || usage_error '--front-url needs a URL'; FRONT_URL=$2; shift 2 ;;
    --compute-url) (($# >= 2)) || usage_error '--compute-url needs a URL'; COMPUTE_URL=$2; shift 2 ;;
    --llama-server) (($# >= 2)) || usage_error '--llama-server needs a path'; LLAMA_SERVER=$2; shift 2 ;;
    --machine-name) (($# >= 2)) || usage_error '--machine-name needs a name'; MACHINE_NAME=$2; shift 2 ;;
    --voice-model) (($# >= 2)) || usage_error '--voice-model needs a model'; VOICE_MODEL=$2; shift 2 ;;
    --platform-services) (($# >= 2)) || usage_error '--platform-services needs a list or none'; PLATFORM_SERVICES=$2; shift 2 ;;
    --start-new) START_NEW=1; shift ;;
    --keep-existing-units) KEEP_EXISTING_UNITS=1; shift ;;
    --uv) (($# >= 2)) || usage_error '--uv needs a path'; UV=$2; shift 2 ;;
    --embedded) EMBEDDED=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --root) (($# >= 2)) || usage_error '--root needs a path'; ROOT=$(cd -- "$2" && pwd); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
done

ui_init
: "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/install-suite-$(date +%Y%m%d-%H%M%S).log}"
ui_log_init 2>/dev/null || OWG_LOG=/dev/null
export OWG_LOG
trap 'on_error $? "${BASH_SOURCE[0]:-}" $LINENO "$BASH_COMMAND"' ERR
detect_os
detect_package_manager

[[ -f $ROOT/pyproject.toml && -f $ROOT/scripts/install-suite-support.py ]] || die "$ROOT is not an openWeightGym checkout: it has no pyproject.toml or scripts/install-suite-support.py." \
  "Point --root at the openWeightGym folder, or run this script from inside one" \
  "Or install openWeightGym afresh with its install.sh, which fetches the code"
[[ $TOPOLOGY =~ ^(keep|all|front|compute)$ ]] || usage_error 'topology must be all, front, or compute'
if [[ $TOPOLOGY == compute && -z $FRONT_URL ]]; then
  die "A GPU node needs --front-url, the console machine's turnstile address." \
    "Pass it, for example: --topology compute --front-url http://192.168.1.10:8442" \
    "Or let install.sh pair this node with a code from the console: install.sh --join http://CONSOLE:8440 --code CODE"
fi
[[ -z $FRONT_URL || $FRONT_URL =~ ^https?://[^/[:space:]]+/?$ ]] || usage_error '--front-url must look like http://192.168.1.10:8442'
[[ -z $COMPUTE_URL || $COMPUTE_URL =~ ^https?://[^/[:space:]]+/?$ ]] || usage_error '--compute-url must look like http://192.168.1.20:8442'
[[ -z $UV || -x $UV ]] || die "--uv $UV is not a program." \
  "Pass uv itself, such as --uv ~/.local/bin/uv (command -v uv shows where it is)" "Or leave --uv out, and python3 -m venv makes the environment"
[[ -z $MACHINE_NAME || $MACHINE_NAME =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || usage_error '--machine-name is lowercase letters, digits and dashes'

if [[ $TOPOLOGY != keep && -z $ADDRESS ]]; then
  detect_address
  ADDRESS=$HOST_ADDRESS
fi
[[ $DATABASE =~ ^(auto|keep|sqlite|postgresql)$ ]] || usage_error 'database must be keep, sqlite, or postgresql'
[[ -z $PRESET || $PRESET =~ ^(compute|front|all)$ ]] || usage_error 'preset must be compute, front, or all'
[[ $STRATA_ACTION =~ ^(install|skip|keep)$ ]] || usage_error 'strata must be install, skip, or keep'
[[ $STRATA_HOST_RAM_MIB =~ ^[1-9][0-9]{0,8}$ ]] || usage_error 'strata host RAM budget must be a positive whole number of MiB'
[[ $SDCPP_ACTION =~ ^(install|skip|keep)$ ]] || usage_error 'sdcpp must be install, skip, or keep'
[[ $SDCPP_BACKEND =~ ^(cuda|vulkan|cpu)$ ]] || usage_error 'sdcpp backend must be cuda, vulkan, or cpu'
[[ $AUDIOCPP_ACTION =~ ^(install|skip|keep)$ ]] || usage_error 'audiocpp must be install, skip, or keep'
[[ $WHISPERCPP_ACTION =~ ^(install|skip|keep)$ ]] || usage_error 'whispercpp must be install, skip, or keep'
[[ $AUDIOCPP_BACKEND =~ ^(cuda|vulkan|cpu)$ ]] || usage_error 'audiocpp backend must be cuda, vulkan, or cpu'
[[ $WHISPERCPP_BACKEND =~ ^(cuda|vulkan|cpu)$ ]] || usage_error 'whispercpp backend must be cuda, vulkan, or cpu'
[[ $CLASSIFIER_TOOLS_ACTION =~ ^(install|skip|keep)$ ]] || usage_error 'classifier-tools must be install, skip, or keep'
for name in "${!DSNS[@]}"; do
  [[ $name =~ ^(core|telemetry|accounts|roles|chat|benchmarks|plugins|alerts|schedules)$ ]] || usage_error "unknown DSN name: $name (the names are core, telemetry, accounts, roles, chat, benchmarks, plugins, alerts and schedules)"
done

has_service() {
  local wanted=$1 item
  for item in "${SELECTED[@]:-}"; do [[ $item == "$wanted" ]] && return 0; done
  return 1
}

valid_service() {
  local wanted=$1 item
  for item in "${SERVICES[@]}"; do [[ $item == "$wanted" ]] && return 0; done
  return 1
}

add_service() {
  valid_service "$1" || usage_error "unknown service: $1 (the services are ${SERVICES[*]}, or the groups compute, front and all)"
  has_service "$1" || SELECTED+=("$1")
}

parse_services() {
  local value=${1// /} item service
  SELECTED=()
  IFS=',' read -ra requested <<< "$value"
  for item in "${requested[@]}"; do
    case "$item" in
      compute) for service in "${COMPUTE[@]}"; do add_service "$service"; done ;;
      front) for service in "${FRONT[@]}"; do add_service "$service"; done ;;
      all) for service in "${SERVICES[@]}"; do add_service "$service"; done ;;
      ''|none) ;;
      *) add_service "$item" ;;
    esac
  done
}

unit_is_installed() {
  local name=$1 alias=
  [[ -e $UNIT_DIR/$name.service ]] && return 0
  case "$name" in toollocker) alias=tooldoor;; weightroom) alias=frontdesk;; esac
  [[ -n $alias && -e $UNIT_DIR/$alias.service ]] && return 0
  command -v systemctl >/dev/null || return 1
  systemctl --user list-unit-files "$name.service" --no-legend 2>/dev/null | grep -q "^$name.service" && return 0
  [[ -n $alias ]] && systemctl --user list-unit-files "$alias.service" --no-legend 2>/dev/null | grep -q "^$alias.service"
}

DETECTED=()
for service in "${SERVICES[@]}"; do unit_is_installed "$service" && DETECTED+=("$service"); done

join_by_comma() { local IFS=,; printf '%s' "$*"; }
ask_yes_no() {
  local prompt=$1 default=${2:-n}
  if ((NON_INTERACTIVE)); then [[ $default == y ]]; return; fi
  ui_confirm "$prompt" "$default"
}

if [[ -n $SERVICE_ARGUMENT ]]; then
  parse_services "$SERVICE_ARGUMENT"
elif ((${#DETECTED[@]})); then
  SELECTED=("${DETECTED[@]}")
elif [[ -n $PRESET ]]; then
  parse_services "$PRESET"
elif ((NON_INTERACTIVE)); then
  die 'No openWeightGym services are installed here, and none were named.' \
    'Name them: --services compute, --services front, --services all, or a list such as --services loadcoach,spotter'
else
  ui_info 'No installed openWeightGym service units were found.'
  choice=0
  ui_choose choice 'Which programs should run on this machine?' 0 \
    'All of them|the console and a GPU node on this one machine' \
    'The console|weightroom, turnstile, toollocker and voice-gateway' \
    'A GPU node|loadcoach, spotter and voice'
  PRESETS=(all front compute)
  parse_services "${PRESETS[choice]}"
fi

((${#SELECTED[@]})) || die 'No service was selected, so there is nothing to install.' \
  'Name at least one: --services compute, front, all, or a list such as loadcoach,spotter'

if ((!NON_INTERACTIVE && ${#DETECTED[@]})); then
  ui_info "Detected and preselected: $(join_by_comma "${DETECTED[@]}")"
fi
if ((!NON_INTERACTIVE)); then
  declare -A ABOUT=(
    [loadcoach]='runs the models (GPU node)' [toollocker]='authenticated MCP tool gateway (console)'
    [spotter]='watches the GPU node' [voice]='speech recognition and voices (GPU node)'
    [weightroom]='the web console' [turnstile]='the way in: keys and routing (console)'
    [voice-gateway]='browser and phone voice (console)')
  rows=()
  for service in "${SERVICES[@]}"; do
    if has_service "$service"; then rows+=("$service|${ABOUT[$service]}|on"); else rows+=("$service|${ABOUT[$service]}|off"); fi
  done
  ticked=''
  ui_checklist ticked 'Which programs should be installed?' "${rows[@]}"
  SELECTED=()
  for index in $ticked; do SELECTED+=("${SERVICES[index]}"); done
  ((${#SELECTED[@]})) || die 'No service was ticked, so there is nothing to install.' 'Run this again and tick at least one: space ticks, enter goes on'
fi

SELECTED_CSV=$(join_by_comma "${SELECTED[@]}")
section 'Warm-up' 'What is installed here'
for service in "${SELECTED[@]}"; do
  unit=$UNIT_DIR/$service.service
  if [[ ! -e $unit && ! -L $unit ]]; then
    case "$service" in
      toollocker) [[ -e $UNIT_DIR/tooldoor.service || -L $UNIT_DIR/tooldoor.service ]] && unit=$UNIT_DIR/tooldoor.service ;;
      weightroom) [[ -e $UNIT_DIR/frontdesk.service || -L $UNIT_DIR/frontdesk.service ]] && unit=$UNIT_DIR/frontdesk.service ;;
    esac
  fi
  if [[ -L $unit && $(readlink "$unit") == /dev/null ]]; then
    ui_warn "$service: existing unit is masked; the installer will replace and unmask it"
  elif [[ -e $unit || -L $unit ]]; then
    command_line=$(sed -n 's/^ExecStart=//p' "$unit" 2>/dev/null | head -1 || true)
    expanded_command=${command_line//\%h/$HOME}
    expanded_command=${expanded_command#\"}
    existing_root=
    if [[ $expanded_command == */.venv/bin/* ]]; then
      existing_root=${expanded_command%%/.venv/bin/*}
    elif [[ $expanded_command =~ --config[[:space:]]+([^[:space:]]+) ]]; then
      existing_root=$(dirname -- "$(dirname -- "${BASH_REMATCH[1]}")")
    fi
    if [[ -n $existing_root ]]; then
      existing_list=${EXISTING_INSTALL_SERVICES[$existing_root]:-}
      EXISTING_INSTALL_SERVICES[$existing_root]=${existing_list:+$existing_list,}$service
    fi
    if [[ -n $command_line && $expanded_command == "$ROOT"/* ]]; then
      ui_ok "$service: unit runs from $ROOT"
    elif [[ -n $command_line ]]; then
      ui_warn "$service: unit currently uses $command_line; it will be backed up and rendered for $ROOT"
    else
      ui_warn "$service: existing unit has no ExecStart; it will be backed up and replaced"
    fi
  else
    ui_ok "$service: new service unit"
  fi
done
# Drop-ins apply to whatever unit has their name: another installation's would run its code here.
detect_units "$ROOT" "$UNIT_DIR"
if ((${#UNITS_DROPINS[@]})) && ((!CHECK_ONLY)); then
  units_retire_steps
  die "Leftover systemd drop-ins would change what openWeightGym's services run:
$(for conf in "${UNITS_DROPINS[@]}"; do printf '  %s\n' "$(shell_path "$conf")"; done)" "${UNITS_STEPS[@]}"
fi
for conf in "${UNITS_DROPINS[@]}"; do ui_warn "drop-in $(shell_path "$conf") changes what its service runs"; done
if [[ -x $ROOT/.venv/bin/python ]]; then
  if "$ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))'; then
    ui_ok "virtual environment: $("$ROOT/.venv/bin/python" --version 2>&1)"
    CHECK_PYTHON=$ROOT/.venv/bin/python
  else
    die "$(shell_path "$ROOT/.venv") uses Python older than 3.13, which openWeightGym needs." "$(venv_aside)"
  fi
else
  ui_ok 'virtual environment: new environment will be created'
  CHECK_PYTHON=python3
  # With uv the system may have no Python of its own: use the one uv keeps.
  if [[ -n $UV ]]; then CHECK_PYTHON=$("$UV" python find 3.13 2>/dev/null) ||
    die 'uv has no Python 3.13 to make the environment with.' "Fetch it (from github.com): $(shell_path "$UV") python install 3.13"; fi
fi
PY=$CHECK_PYTHON
if [[ $DATABASE == postgresql ]]; then
  repair_allowed=1
  for existing_root in "${!EXISTING_INSTALL_SERVICES[@]}"; do
    [[ $existing_root == "$ROOT" ]] || repair_allowed=0
  done
  if ((repair_allowed)); then repair_postgresql; fi
fi
CHECK_RUNNER=("$CHECK_PYTHON")
# A retained PostgreSQL configuration may outlive its old virtual environment.
# Keep audit dependencies in uv's cache instead of modifying the system Python.
if ! "$CHECK_PYTHON" -c 'import psycopg' >/dev/null 2>&1 && "$CHECK_PYTHON" - "$ROOT/settings" <<'PY'
from pathlib import Path
import sys
import tomllib
for path in Path(sys.argv[1]).glob('*.toml'):
    try:
        settings = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if any(store.get('engine') == 'postgresql' for store in settings.get('databases', {}).get('stores', {}).values() if isinstance(store, dict)):
        raise SystemExit(0)
raise SystemExit(1)
PY
then
  [[ -n $UV ]] || die 'Checking the PostgreSQL settings already here needs psycopg.' \
    'Pass the uv that installed openWeightGym: --uv ~/.local/bin/uv' 'Or install the PostgreSQL extra into its environment: .venv/bin/pip install "psycopg[binary]"'
  CHECK_RUNNER=("$UV" run --no-project --python "$CHECK_PYTHON" --with 'psycopg[binary]>=3.2,<4' python)
fi
check_python() { "${CHECK_RUNNER[@]}" "$@"; }
ui_info 'Existing-install compatibility check'
initial_audit=(--root "$ROOT" --services "$SELECTED_CSV")
((CHECK_ONLY)) && initial_audit+=(--require-pg-tools)
if ! audit check_python "${initial_audit[@]}"; then
  die "The settings already in $(shell_path "$ROOT/settings") did not pass the compatibility check, so no service was changed." \
    "The lines marked ✘ above say what is wrong: put that right in those files"
fi
for existing_root in "${!EXISTING_INSTALL_SERVICES[@]}"; do
  [[ $existing_root == "$ROOT" ]] && continue
  if [[ ! -d $existing_root ]]; then
    detect_units "$ROOT" "$UNIT_DIR"
    units_retire_steps
    die "Services with these names run an installation that is gone: $existing_root" \
      "Retire them; there is nothing of theirs left to keep:" "${UNITS_STEPS[@]}"
  fi
  ui_info "Compatibility check for existing installation at $existing_root"
  if ! audit check_python \
      --root "$existing_root" --services "${EXISTING_INSTALL_SERVICES[$existing_root]}"; then
    detect_units "$ROOT" "$UNIT_DIR"
    units_retire_steps
    die "The installation already at $existing_root did not pass the compatibility check, so nothing was changed." \
      "The lines marked ✘ above say what is wrong with it" \
      "To keep it, put that right and update it with its own installer: $(shell_path "$existing_root/install.sh")" \
      "To replace it with this one, retire its services first; its folder, settings and data are left as they are:" \
      "${UNITS_STEPS[@]}"
  fi
  detect_units "$ROOT" "$UNIT_DIR"
  units_retire_steps
  die "Services with these names already run another installation, from $existing_root." \
    "To keep that one, update it with its own installer instead of this one" \
    "To replace it, retire its services first; its folder, settings and data are left as they are:" \
    "${UNITS_STEPS[@]}"
done

if ((CHECK_ONLY)); then
  ui_info "Installed openWeightGym units: ${DETECTED[*]:-(none)}"
  ui_info "Selected openWeightGym units: ${SELECTED[*]}"
  # The optional runtimes are reported, not required: a broken one is something to fix, not a stop.
  if has_service loadcoach; then
    "$PY" "$ROOT/scripts/install-sdcpp.py" --root "$ROOT" --check ||
      ui_warn "The image runtime (sdcpp) in settings/loadcoach.toml does not work" "reinstall it: $(shell_path "$ROOT/scripts/install-sdcpp.sh") --root $(shell_path "$ROOT")"
    "$PY" "$ROOT/soundroom/install.py" --root "$ROOT" --check ||
      ui_warn "The audio runtime (audio.cpp) in settings/loadcoach.toml does not work" "reinstall it: $(shell_path "$ROOT/scripts/install-audiocpp.sh") --root $(shell_path "$ROOT")"
    "$PY" "$ROOT/engines/whisper_cpp/install.py" --root "$ROOT" --check ||
      ui_warn "The audio runtime (whisper.cpp) in settings/loadcoach.toml does not work" "reinstall it: $(shell_path "$ROOT/scripts/install-whispercpp.sh") --root $(shell_path "$ROOT")"
  fi
  exit 0
fi

if has_service loadcoach; then
  if ((!NON_INTERACTIVE && !STRATA_EXPLICIT)); then
    if ask_yes_no "Install Strata (CUDA text models; allows up to $STRATA_HOST_RAM_MIB MiB host RAM)? Existing support is kept if declined." n; then
      STRATA_ACTION=install
    fi
  fi
  if ((!NON_INTERACTIVE && !SDCPP_EXPLICIT)); then
    if ask_yes_no 'Install optional sdcpp image-generation support? Existing support is kept if declined.' n; then
      SDCPP_ACTION=install
    fi
  fi
  if ((!NON_INTERACTIVE && !AUDIOCPP_EXPLICIT)); then
    if ask_yes_no 'Install optional audio.cpp audio support (music, sounds, speech)? Existing support is kept if declined.' n; then
      AUDIOCPP_ACTION=install
    fi
  fi
  if ((!NON_INTERACTIVE && !WHISPERCPP_EXPLICIT)); then
    if ask_yes_no 'Install optional whisper.cpp speech recognition and translation? Existing support is kept if declined.' n; then
      WHISPERCPP_ACTION=install
    fi
  fi
  if ((!NON_INTERACTIVE && !CLASSIFIER_TOOLS_EXPLICIT)); then
    if ask_yes_no 'Install Classifier conversion tools (CPU; matches the configured llama.cpp build)? Existing tools are kept if declined.' n; then
      CLASSIFIER_TOOLS_ACTION=install
    fi
  fi
elif [[ $STRATA_ACTION == install ]]; then
  die 'Strata requires loadcoach on this machine.' 'Install loadcoach too (--services compute), or leave out --strata install'
elif [[ $SDCPP_ACTION == install ]]; then
  die 'The image runtime (sdcpp) runs beside loadcoach, on a GPU node, and loadcoach is not being installed here.' \
    'Install loadcoach too (--services compute), or leave out --sdcpp install'
elif [[ $AUDIOCPP_ACTION == install ]]; then
  die 'The audio runtime (audio.cpp) runs beside loadcoach, on a GPU node, and loadcoach is not being installed here.' \
    'Install loadcoach too (--services compute), or leave out --audiocpp install'
elif [[ $WHISPERCPP_ACTION == install ]]; then
  die 'The audio runtime (whisper.cpp) runs beside loadcoach, on a GPU node, and loadcoach is not being installed here.' \
    'Install loadcoach too (--services compute), or leave out --whispercpp install'
elif [[ $CLASSIFIER_TOOLS_ACTION == install ]]; then
  die 'Classifier conversion tools require loadcoach and its configured llama.cpp on this machine.' \
    'Install them on your compute node: owg install --classifier-tools install'
fi

read -r CURRENT_DATABASE DATABASE_CONTENT <<< "$(check_python "$ROOT/scripts/install-suite-support.py" state --root "$ROOT" --services "$SELECTED_CSV")"
if [[ $DATABASE == auto ]]; then
  if ((NON_INTERACTIVE)); then DATABASE=keep; else
    ui_choose choice 'Where should openWeightGym keep its data?' 0 \
      "Keep current|$CURRENT_DATABASE" 'SQLite|files in data/, nothing else to run' 'PostgreSQL|a database server, local or elsewhere'
    DATABASES=(keep sqlite postgresql)
    DATABASE=${DATABASES[choice]}
  fi
fi
[[ $DATABASE =~ ^(keep|sqlite|postgresql)$ ]] || usage_error 'database must be keep, sqlite, or postgresql'
if [[ $DATABASE != keep && $DATABASE != "$CURRENT_DATABASE" && $DATABASE_CONTENT == populated ]]; then
  die "The selected stores already hold data in $CURRENT_DATABASE, and this installer does not move data between database engines." \
    "Keep the data where it is: --database keep" \
    "To move it, follow docs/database-storage.md first, then run this again"
fi
if [[ $DATABASE != postgresql && (${#DSNS[@]} -gt 0 || $PROVISION_POSTGRESQL == 1) ]]; then
  usage_error 'PostgreSQL DSNs and provisioning require --database postgresql'
fi

USES_POSTGRESQL=0
if [[ $DATABASE == postgresql || ($DATABASE == keep && ($CURRENT_DATABASE == postgresql || $CURRENT_DATABASE == mixed)) ]]; then
  USES_POSTGRESQL=1
fi
if ((USES_POSTGRESQL && !NON_INTERACTIVE && !INSTALL_POSTGRESQL)); then
  ask_yes_no 'Install local PostgreSQL server and client packages?' n && INSTALL_POSTGRESQL=1
fi
if [[ $DATABASE == postgresql && $NON_INTERACTIVE == 0 && $PROVISION_POSTGRESQL == 0 && ${#DSNS[@]} -eq 0 ]]; then
  ask_yes_no 'Create separate local databases and owner-only DSN files for openWeightGym?' y && {
    PROVISION_POSTGRESQL=1
    INSTALL_POSTGRESQL=1
  }
fi
if ((USES_POSTGRESQL && !NON_INTERACTIVE && !INSTALL_PGVECTOR)); then
  ask_yes_no 'Install pgvector for possible future use?' n && { INSTALL_PGVECTOR=1; INSTALL_POSTGRESQL=1; }
fi

# New gateways inherit the control node's accounts. Existing compute-owned settings remain supported.
TOOLLOCKER_FRONT=0
if has_service toollocker; then
  TOOLLOCKER_FRONT=1
  if [[ -f $ROOT/settings/toollocker.toml ]]; then
    TOOLLOCKER_FRONT=$(check_python - "$ROOT/settings/toollocker.toml" <<'PY'
from pathlib import Path
import sys
import tomllib
print(int(bool(tomllib.loads(Path(sys.argv[1]).read_text()).get('front'))))
PY
)
  fi
fi
REQUIRED_DSNS=()
if has_service loadcoach || has_service spotter || { has_service toollocker && ((!TOOLLOCKER_FRONT)); }; then REQUIRED_DSNS+=(core); fi
has_service spotter && REQUIRED_DSNS+=(telemetry)
if has_service weightroom || has_service turnstile || ((TOOLLOCKER_FRONT)); then REQUIRED_DSNS+=(accounts roles chat benchmarks plugins alerts schedules); fi

# What to do instead when PostgreSQL cannot be set up here: the last of a failure's steps.
PG_INSTEAD=("Or set PostgreSQL up yourself (docs/database-storage.md) so that 'sudo -u postgres psql' answers, then run this again"
            "Or keep the data in SQLite, which needs no server: $OWG_RETRY_COMMAND --database sqlite")

# Run as PostgreSQL's own account, from a folder it can read (yours may not be).
as_postgres() { (cd / && sudo -n -u postgres "$@"); }

# The packages of a local PostgreSQL server and its client programs (psql, createdb).
postgresql_packages() {
  case "$PKG_MANAGER" in
    apt) echo postgresql postgresql-client ;;
    dnf|zypper) echo postgresql-server postgresql ;;
    pacman) echo postgresql ;;
  esac
}

# Install the server and, to provision it, start it. The package managers are told never to ask:
# with `curl | bash` there is nobody on their input to answer.
install_postgresql_packages() {
  ((INSTALL_POSTGRESQL)) || return 0
  local -a packages
  read -r -a packages <<<"$(postgresql_packages)"
  ((${#packages[@]})) || die "This installer does not know how to install PostgreSQL on $OS_NAME." "${PG_INSTEAD[@]}"
  ((DRY_RUN)) || ui_sudo "installing and setting up PostgreSQL"
  if [[ $PKG_MANAGER == apt ]]; then
    try_step 'Refreshing the package list' sudo -n apt-get update ||
      die "apt could not refresh its package lists." "Run it yourself to see why: sudo apt-get update" "${PG_INSTEAD[@]}"
  fi
  try_step "Installing PostgreSQL ($PKG_MANAGER: ${packages[*]})" sudo -n "${PKG_INSTALL[@]}" "${packages[@]}" ||
    die "PostgreSQL could not be installed with $PKG_MANAGER." "?$(pkg_refresh_hint)" \
      "Check it installs by hand: $(pkg_command "${packages[@]}")" "${PG_INSTEAD[@]}"
  ((PROVISION_POSTGRESQL && !DRY_RUN)) || return 0
  start_postgresql
}

# The newest initdb here: on PATH (Arch, Fedora), or under the server's own folder (openSUSE keeps
# it in /usr/lib/postgresql17/bin, Debian in /usr/lib/postgresql/17/bin).
initdb_program() {
  local found
  found=$(command -v initdb) || found=$(ls -1d /usr/lib/postgresql*/bin/initdb /usr/lib/postgresql/*/bin/initdb 2>/dev/null | sort -V | tail -1)
  [[ -n $found ]] && printf '%s\n' "$found"
}

# A new server is made the way its family expects, with password logins over TCP (what the DSN
# files use) and peer logins for the postgres account, then started and waited for.
start_postgresql() {
  [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null ||
    die "PostgreSQL is installed, but systemd is not running here to start it." \
      "Start PostgreSQL yourself, so that 'sudo -u postgres psql' answers, then run this again" "${PG_INSTEAD[1]}"
  local data='' version='' initdb='' initdb_options=(--auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8)
  case "$PKG_MANAGER" in
    pacman) data=/var/lib/postgres/data ;;
    dnf|zypper) data=/var/lib/pgsql/data ;;
  esac
  if [[ -n $data ]] && ! sudo -n test -f "$data/PG_VERSION" </dev/null; then
    if [[ $PKG_MANAGER == dnf ]] && command -v postgresql-setup >/dev/null; then
      try_step 'Creating the PostgreSQL database cluster' \
        sudo -n env "PGSETUP_INITDB_OPTIONS=${initdb_options[*]}" postgresql-setup --initdb
    else
      initdb=$(initdb_program) || die "PostgreSQL is installed, but its initdb program is not to be found." \
        "Install the server package: $(pkg_command "$(postgresql_packages)")" "${PG_INSTEAD[@]}"
      try_step 'Creating the PostgreSQL database cluster' as_postgres "$initdb" -D "$data" "${initdb_options[@]}" --locale=C.UTF-8
    fi || die "PostgreSQL's database cluster could not be created in $data." \
      "Create it yourself: sudo -u postgres ${initdb:-initdb} -D $data ${initdb_options[*]}" "${PG_INSTEAD[@]}"
  fi
  # Debian and Ubuntu make a cluster when the package is installed, unless that is turned off.
  if [[ $PKG_MANAGER == apt ]] && command -v pg_lsclusters >/dev/null && [[ -z $(pg_lsclusters -h 2>/dev/null) ]]; then
    version=$(find /usr/lib/postgresql -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | sort -V | tail -1)
    try_step 'Creating the PostgreSQL database cluster' sudo -n pg_createcluster "$version" main ||
      die "PostgreSQL's database cluster could not be created." "Create it yourself: sudo pg_createcluster $version main" "${PG_INSTEAD[@]}"
  fi
  try_step 'Starting PostgreSQL' sudo -n systemctl enable --now postgresql.service ||
    die "PostgreSQL did not start." "See why: sudo journalctl -u postgresql -n 50 --no-pager" \
      "Put that right and start it: sudo systemctl enable --now postgresql" "${PG_INSTEAD[@]}"
  try_step 'Waiting for PostgreSQL to answer' wait_for_postgresql ||
    die "PostgreSQL started but does not answer." "See why: sudo journalctl -u postgresql -n 50 --no-pager" \
      "Check it answers: sudo -u postgres psql -c 'SELECT 1'" "${PG_INSTEAD[@]}"
}

wait_for_postgresql() {
  local i
  for ((i = 0; i < 30; i++)); do
    as_postgres psql -Atqc 'SELECT 1' postgres >/dev/null 2>&1 && return 0
    sleep 1
  done
  as_postgres psql -Atqc 'SELECT 1' postgres
}

install_pgvector_package() {
  ((INSTALL_PGVECTOR)) || return 0
  local package='' major=''
  major=$(psql --version 2>/dev/null | sed -nE 's/.* ([0-9]+)(\.[0-9]+)*.*/\1/p')
  case "$PKG_MANAGER" in
    apt)
      if [[ -n $major ]] && apt-cache show "postgresql-$major-pgvector" >/dev/null 2>&1; then
        package="postgresql-$major-pgvector"
      elif apt-cache show postgresql-pgvector >/dev/null 2>&1; then
        package=postgresql-pgvector
      else
        package=$(apt-cache pkgnames 2>/dev/null | grep -E '^postgresql-[0-9]+-pgvector$' | sort -V | tail -1 || true)
      fi ;;
    dnf) package=pgvector ;;
    zypper) package=postgresql${major}-pgvector ;;
    pacman) package=pgvector ;;
  esac
  local skip="pgvector is optional: run this again without --pgvector to go on without it"
  [[ -n $package ]] || die "No pgvector package is known for $OS_NAME." "$skip"
  try_step "Installing pgvector ($package)" sudo -n "${PKG_INSTALL[@]}" "$package" ||
    die "pgvector could not be installed with $PKG_MANAGER." "?$(pkg_refresh_hint)" "$skip"
}

provision_postgresql() {
  ((PROVISION_POSTGRESQL)) || return 0
  ((DRY_RUN)) && { note 'DRY RUN: provision local PostgreSQL role, separate databases, and mode-600 DSN files'; return 0; }
  command -v psql >/dev/null && command -v createdb >/dev/null ||
    die "PostgreSQL's client programs (psql, createdb) are not installed." \
      "Install them: $(pkg_command "$(postgresql_packages)")" "${PG_INSTEAD[@]}"
  local host role prefix password key database dsn_dir existing port
  host=$(uname -n | cut -d. -f1 | tr '[:upper:]-' '[:lower:]_' | tr -cd 'a-z0-9_')
  role="suite_${USER//[^a-zA-Z0-9_]/_}_${host:-host}"
  role=${role:0:60}
  prefix=${SUITE_DATABASE_PREFIX:-${role}}
  dsn_dir=$ROOT/keys/postgresql
  install -d -m 700 "$dsn_dir"
  repair_postgresql
  port=$(as_postgres psql -Atqc 'SHOW port' postgres) ||
    die "The postgres account cannot reach the PostgreSQL server." "Check it answers: sudo -u postgres psql -c 'SELECT 1'" "${PG_INSTEAD[@]}"
  [[ $port =~ ^[0-9]+$ ]] || die "PostgreSQL answered, but not with its port (it said: ${port:-nothing})." \
    "Check it answers: sudo -u postgres psql -c 'SHOW port'" "${PG_INSTEAD[@]}"
  existing=$(as_postgres psql -Atqc "SELECT 1 FROM pg_roles WHERE rolname = '$role'" postgres) ||
    die "PostgreSQL did not answer a question about its accounts." "Check it answers: sudo -u postgres psql -c 'SELECT 1'" "${PG_INSTEAD[@]}"
  if [[ $existing == 1 ]]; then
    local complete=1 suffix=2
    for key in "${REQUIRED_DSNS[@]}"; do
      [[ -f $dsn_dir/$key.dsn ]] || complete=0
    done
    if ((complete)); then
      for key in "${REQUIRED_DSNS[@]}"; do DSNS[$key]=$dsn_dir/$key.dsn; done
      note "Using existing local PostgreSQL role and DSN files for $role."
      check_postgresql_login "${DSNS[${REQUIRED_DSNS[0]}]}"
      return 0
    fi
    while as_postgres psql -Atqc "SELECT 1 FROM pg_roles WHERE rolname = '${role:0:55}_$suffix'" postgres | grep -q 1; do
      ((suffix++))
    done
    role="${role:0:55}_$suffix"
    prefix=$role
  fi
  password=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')
  # Stored as SCRAM even where the server's default is still md5 (PostgreSQL 13 and older):
  # a SCRAM password signs in through both scram-sha-256 and md5 rules.
  printf 'SET password_encryption = '\''scram-sha-256'\'';\nCREATE ROLE "%s" LOGIN PASSWORD '\''%s'\'';\n' "$role" "$password" |
    as_postgres psql -v ON_ERROR_STOP=1 -q postgres ||
    die "PostgreSQL would not make the account $role for openWeightGym." \
      "Its message is just above; check the postgres account can make accounts: sudo -u postgres psql -c 'SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user'" \
      "${PG_INSTEAD[@]}"
  for key in "${REQUIRED_DSNS[@]}"; do
    # A retry or a newly required store must not redirect existing stores to empty databases.
    if [[ -f $dsn_dir/$key.dsn ]]; then
      DSNS[$key]=$dsn_dir/$key.dsn
      continue
    fi
    # Leave room for the store name so long host names/prefixes cannot collide.
    database="${prefix:0:48}_${key}"
    as_postgres createdb --owner "$role" "$database" ||
      die "PostgreSQL would not make the database $database." \
        "Its message is just above (a full disk, often: df -h)" \
        "Once that is put right, run this again: databases already made are kept" "${PG_INSTEAD[@]}"
    printf 'postgresql://%s:%s@127.0.0.1:%s/%s\n' "$role" "$password" "$port" "$database" > "$dsn_dir/$key.dsn"
    chmod 600 "$dsn_dir/$key.dsn"
    DSNS[$key]=$dsn_dir/$key.dsn
  done
  unset password
  check_postgresql_login "${DSNS[${REQUIRED_DSNS[0]}]}"
}

# The programs sign in with a password over TCP. A server whose rules allow only ident or peer
# logins there (as some distributions set up) refuses them: say how to let them in.
check_postgresql_login() {  # check_postgresql_login DSN-FILE
  local dsn hba
  dsn=$(<"$1")
  [[ $dsn =~ ^postgresql://([^:/@]+):([^@]+)@([^:/]+):([0-9]+)/(.+)$ ]] || return 0
  if PGPASSWORD=${BASH_REMATCH[2]} PGCONNECT_TIMEOUT=5 psql -X -w -h "${BASH_REMATCH[3]}" -p "${BASH_REMATCH[4]}" \
      -U "${BASH_REMATCH[1]}" -d "${BASH_REMATCH[5]}" -Atqc 'SELECT 1' >>"${OWG_LOG:-/dev/null}" 2>&1; then
    return 0
  fi
  hba=$(as_postgres psql -Atqc 'SHOW hba_file' postgres 2>/dev/null) || hba='pg_hba.conf'
  die "PostgreSQL refuses the password sign-in openWeightGym uses (over TCP to ${BASH_REMATCH[3]})." \
    "Let local password sign-ins in: as root, put these lines above the other 'host' lines of $hba
host  all  all  127.0.0.1/32  scram-sha-256
host  all  all  ::1/128       scram-sha-256" \
    "Reload PostgreSQL: sudo systemctl reload postgresql" "${PG_INSTEAD[@]}"
}

collect_dsns() {
  [[ $DATABASE == postgresql ]] || return 0
  local key value
  for key in "${REQUIRED_DSNS[@]}"; do
    [[ -n ${DSNS[$key]:-} ]] && continue
    if ((DRY_RUN && PROVISION_POSTGRESQL)); then
      DSNS[$key]=$ROOT/keys/postgresql/$key.dsn
    elif ((NON_INTERACTIVE)); then
      die "PostgreSQL needs a DSN file for the $key store." \
        "Pass one for each store: --dsn $key=FILE, a file (mode 600) holding postgresql://USER:PASSWORD@HOST:PORT/DATABASE" \
        "Or let this script make the databases: --provision-postgresql"
    else
      read -r -p "Owner-only PostgreSQL DSN file for $key: " value
      [[ -n $value ]] || die "PostgreSQL needs a DSN file for the $key store." \
        "Run this again and give one: a file (mode 600) holding postgresql://USER:PASSWORD@HOST:PORT/DATABASE" \
        "Or let this script make the databases: --provision-postgresql"
      DSNS[$key]=$value
    fi
  done
  for key in "${REQUIRED_DSNS[@]}"; do
    DSNS[$key]=$(realpath -m -- "${DSNS[$key]}")
    if ((!DRY_RUN)); then
      [[ -f ${DSNS[$key]} ]] || die "The DSN file for the $key store does not exist: ${DSNS[$key]}" \
        "Check the path, or make the file: mode 600, holding postgresql://USER:PASSWORD@HOST:PORT/DATABASE"
      [[ -z $(find "${DSNS[$key]}" -maxdepth 0 -perm /077 -print) ]] ||
        die "The DSN file for the $key store can be read by other accounts: ${DSNS[$key]}" "Make it its owner's only: chmod 600 $(shell_path "${DSNS[$key]}")"
    fi
  done
}

if ((!EMBEDDED)); then
  ui_section 'Game plan' 'Installation choices'
  ui_kv Source "$ROOT"
  ui_kv Services "${SELECTED[*]}"
  ui_kv Database "$DATABASE"
  [[ $TOPOLOGY != keep ]] && ui_kv 'This machine' "$TOPOLOGY ($ADDRESS)"
  has_service loadcoach && ui_kv Strata "$STRATA_ACTION (CUDA; host RAM: $STRATA_HOST_RAM_MIB MiB)"
  has_service loadcoach && ui_kv Images "sdcpp $SDCPP_ACTION (backend: $SDCPP_BACKEND; weights downloaded separately)"
  has_service loadcoach && ui_kv Audio "audio.cpp $AUDIOCPP_ACTION (backend: $AUDIOCPP_BACKEND; weights downloaded separately)"
  has_service loadcoach && ui_kv Whisper "whisper.cpp $WHISPERCPP_ACTION (backend: $WHISPERCPP_BACKEND; weights downloaded separately)"
  has_service loadcoach && ui_kv 'Classifier conversion tools' "$CLASSIFIER_TOOLS_ACTION (CPU; follows the configured llama.cpp build)"
  ((INSTALL_POSTGRESQL)) && ui_kv Packages 'PostgreSQL server/client'
  ((INSTALL_PGVECTOR)) && ui_kv Optional 'pgvector'
  if ((START_SERVICES)); then ui_kv Activation 'enable units and start configured existing services'; else ui_kv Activation 'enable units without starting'; fi
fi
if ((!ASSUME_YES && !NON_INTERACTIVE)); then ask_yes_no 'Continue with this installation?' y || exit 0; fi
section 'Setting up' 'Python, settings and services'

install_postgresql_packages
install_pgvector_package
provision_postgresql
collect_dsns

if [[ -x $ROOT/.venv/bin/python ]]; then
  if ! "$ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))'; then
    die "$(shell_path "$ROOT/.venv") uses Python older than 3.13, which openWeightGym needs." "$(venv_aside)"
  fi
elif [[ -n $UV ]]; then
  try_step 'Making the Python environment' "$UV" venv --seed --python 3.13 "$ROOT/.venv" ||
    die "uv could not make the Python environment in $(shell_path "$ROOT/.venv")." \
      "The lines above, from the log, say why: put that right" \
      '?uv fetches Python 3.13 from github.com when this system has none: check it can reach it: curl -sSI https://github.com | head -1' \
      "Remove what was left of it: rm -rf $(shell_path "$ROOT/.venv")"
else
  try_step 'Making the Python environment' python3 -m venv "$ROOT/.venv" ||
    die "Python could not make the environment in $(shell_path "$ROOT/.venv")." \
      "The lines above, from the log, say why: put that right" \
      "$([[ $PKG_MANAGER == apt ]] && printf '?On Debian and Ubuntu its venv module is a package of its own: %s' "$(pkg_command python3-venv)")" \
      "Remove what was left of it: rm -rf $(shell_path "$ROOT/.venv")"
fi
((DRY_RUN)) || PY=$ROOT/.venv/bin/python
if [[ -n $UV ]]; then
  pip_install() { "$UV" pip install --python "$ROOT/.venv/bin/python" "$@"; }
else
  pip_install() { "$ROOT/.venv/bin/python" -m pip install "$@"; }
fi
# Where Python packages come from, for the steps that fetch them.
# Each starts with ?: a guess at the cause, left out when the log names the real one.
PYPI_STEPS=('?Check this machine can reach the Python package index: curl -sSI https://pypi.org/simple/ | head -1'
            '?Behind a proxy? Tell the installer about it: export https_proxy=http://PROXY:PORT http_proxy=http://PROXY:PORT')
try_step 'Bringing pip up to date' pip_install --upgrade 'pip>=26.2.1' ||
  die "pip could not be brought up to date in $(shell_path "$ROOT/.venv")." "The lines above, from the log, say why: put that right" "${PYPI_STEPS[@]}"

extras=()
((USES_POSTGRESQL)) && extras+=(postgresql)
(has_service weightroom || has_service turnstile) && extras+=(benchmarks)
has_service weightroom && extras+=(knowledge)
has_service voice && extras+=(voice)
has_service voice-gateway && extras+=(voice-gateway)
extra_csv=$(join_by_comma "${extras[@]}")
packages=$ROOT
[[ -n $extra_csv ]] && packages="$ROOT[$extra_csv]"
# shellcheck disable=SC2046  # build_packages is a list of package names
try_step "Installing Python packages${extra_csv:+ ($extra_csv)}" pip_install -e "$packages" ||
  die "openWeightGym's Python packages did not install." \
    "The lines above, from the log, say why: put that right" "${PYPI_STEPS[@]}" \
    "?If the log shows a package being built, install the build tools: $(pkg_command $(build_packages))"
try_step 'Compiling' "$ROOT/.venv/bin/python" -m compileall -q "$ROOT/engines" "$ROOT/mirrorwall" "$ROOT/soundroom" "$ROOT/db" "$ROOT/loadcoach" "$ROOT/toollocker" "$ROOT/spotter" "$ROOT/weightroom" "$ROOT/turnstile" "$ROOT/voice" ||
  die "openWeightGym's code in $(shell_path "$ROOT") does not compile." \
    "The lines above name the file and the line" \
    "If you have not changed that file, see what differs from the published code: git -C $(shell_path "$ROOT") status" \
    "Repair the installation, which fetches the code again and keeps your changes aside: owg repair"

if has_service weightroom; then
  try_step 'Building the Voice desktop download' "$ROOT/.venv/bin/python" \
    "$ROOT/scripts/package-voice-desktop.py" "$ROOT/weightroom/static/LoadCoach-Voice.zip" ||
    die 'The Voice desktop download could not be built.' \
      'Resolve the packaging error above, then retry the installation'
fi

mkdir_if_real() { if ((DRY_RUN)); then ui_note "DRY RUN: mkdir -p $*"; else mkdir -p "$@"; fi; }
mkdir_if_real "$ROOT/settings/defaults" "$ROOT/settings/examples" "$ROOT/data" "$ROOT/keys" "$UNIT_DIR"
((DRY_RUN)) || chmod 700 "$ROOT/keys"
# The folder the files tool reads, and the models folder LoadCoach is given, for new settings.
if [[ $TOPOLOGY != keep ]] || ((TOOLLOCKER_FRONT)); then mkdir_if_real "$ROOT/data/shared" "$ROOT/models"; fi

config_for() {
  case "$1" in turnstile) printf 'weightroom';; *) printf '%s' "$1";; esac
}

install_example_config() {
  local config_name=$1 source_config target_config
  source_config=$ROOT/settings/examples/$config_name.toml
  target_config=$ROOT/settings/$config_name.toml
  [[ -f $source_config ]] || die "The example settings $(shell_path "$source_config") are missing: this copy of openWeightGym is incomplete." \
    "Put the file back: git -C $(shell_path "$ROOT") checkout -- settings/examples/$config_name.toml" "Or repair the installation: owg repair"
  if [[ ! -f $target_config ]]; then
    [[ -v WAS_CONFIGURED[$config_name] ]] || WAS_CONFIGURED[$config_name]=0
    if ((DRY_RUN)); then
      ui_note "DRY RUN: render $source_config to $target_config for $ROOT and $HOME ($TOPOLOGY)"
    else
      local voice_options=()
      (has_service voice || has_service voice-gateway) && voice_options=(--voice ${VOICE_MODEL:+--voice-model "$VOICE_MODEL"})
      "$PY" "$ROOT/scripts/install-suite-support.py" render --example "$source_config" --target "$target_config" \
        --root "$ROOT" --home "$HOME" --topology "$TOPOLOGY" --address "${ADDRESS:-127.0.0.1}" \
        --front "$FRONT_URL" --compute "$COMPUTE_URL" --llama-server "$LLAMA_SERVER" --machine-name "$MACHINE_NAME" \
        --platform-services "$PLATFORM_SERVICES" "${voice_options[@]}" ||
        die "The settings file $(shell_path "$target_config") could not be written." \
          "The lines above say why" "Check the folder is yours and has room: ls -ld $(shell_path "$ROOT/settings") && df -h $(shell_path "$ROOT")"
      ui_ok "Wrote settings/$config_name.toml"
    fi
  else
    [[ -v WAS_CONFIGURED[$config_name] ]] || WAS_CONFIGURED[$config_name]=1
  fi
}

# Write the owning settings before the gateway that inherits them.
if ((TOOLLOCKER_FRONT)); then install_example_config weightroom; fi
for service in "${SELECTED[@]}"; do
  config_name=$(config_for "$service")
  [[ $config_name == voice-gateway || $config_name == voice || $config_name == loadcoach || $config_name == spotter || $config_name == toollocker || $config_name == weightroom ]] || continue
  install_example_config "$config_name"
done
if { has_service spotter || { has_service toollocker && ((!TOOLLOCKER_FRONT)); }; } && [[ ! -f $ROOT/settings/loadcoach.toml ]]; then
  ui_info 'Installing LoadCoach settings as the shared identity configuration for Spotter/ToolLocker.'
  install_example_config loadcoach
fi

if has_service loadcoach && [[ -n $LLAMA_SERVER ]]; then
  run "$PY" "$ROOT/scripts/install-suite-support.py" llama-server --root "$ROOT" --executable "$LLAMA_SERVER" ||
    die 'Could not select the installed llama-server.' 'Check its executable path and settings/loadcoach.toml'
fi

restore_configs() {
  local config backup
  for config in "${!CONFIG_BACKUPS[@]}"; do
    backup=${CONFIG_BACKUPS[$config]}
    [[ -f $backup ]] && cp "$backup" "$config"
  done
}

if [[ $DATABASE != keep ]]; then
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  for service in weightroom loadcoach spotter toollocker; do
    if ! has_service "$service"; then
      [[ $service == weightroom ]] && { has_service turnstile || ((TOOLLOCKER_FRONT)); } || continue
    fi
    config=$ROOT/settings/$service.toml
    [[ -f $config ]] || continue
    if ((DRY_RUN)); then
      ui_note "DRY RUN: back up and configure $config for $DATABASE"
      continue
    fi
    # Settings written by this run have nothing of yours to keep a copy of.
    if [[ ${WAS_CONFIGURED[$service]:-1} == 1 ]]; then
      backup=$config.before-install-$timestamp
      cp -p "$config" "$backup"
      CONFIG_BACKUPS[$config]=$backup
    fi
    arguments=(configure --config "$config" --service "$service" --engine "$DATABASE")
    if [[ $DATABASE == postgresql ]]; then
      for key in "${!DSNS[@]}"; do arguments+=(--dsn "$key=${DSNS[$key]}"); done
    fi
    try_step "Configuring $service databases ($DATABASE)" "$PY" "$ROOT/scripts/install-suite-support.py" "${arguments[@]}" ||
      die "The $service settings could not be pointed at the $DATABASE databases." \
        "The lines above, from the log, say why: put that right" \
        "$([[ $DATABASE == postgresql ]] && printf '%s' "?Check PostgreSQL answers: sudo -u postgres psql -c 'SELECT 1'")" \
        "$([[ -n ${CONFIG_BACKUPS[$config]:-} ]] && printf 'Your settings from before this run are in %s' "$(shell_path "${CONFIG_BACKUPS[$config]}")")" \
        "Or keep the data where it is: --database keep"
    # A copy of a file that came out the same is no copy worth keeping.
    if [[ -n ${CONFIG_BACKUPS[$config]:-} ]] && cmp -s "$config" "${CONFIG_BACKUPS[$config]}"; then
      rm -f -- "${CONFIG_BACKUPS[$config]}"
      unset 'CONFIG_BACKUPS[$config]'
    fi
  done
fi

if ((!DRY_RUN)); then
  if ! audit --quiet "$ROOT/.venv/bin/python" --root "$ROOT" --services "$SELECTED_CSV" --require-pg-tools; then
    restore_configs
    die "The new database settings did not pass the compatibility check, so the earlier settings were put back." \
      "The lines marked ✘ above say what is wrong: put that right" \
      "Or keep the data where it is: --database keep"
  fi
fi

# The secrets a fresh installation needs and no example can hold. Each is made only when absent,
# so a reinstall or an imported keys/ folder is kept as it is.
make_secret() {  # FILE: a random machine key, readable by its owner only
  if ((DRY_RUN)); then ui_note "DRY RUN: create $1"; return; fi
  (umask 077 && "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$1")
}
if [[ ! -f $ROOT/keys/machine.key ]] && (has_service turnstile || has_service loadcoach || has_service spotter || has_service toollocker || has_service voice); then
  make_secret "$ROOT/keys/machine.key"
  if [[ $TOPOLOGY == all ]]; then
    ui_ok 'Made keys/machine.key, the key the console and the GPU node sign with'
  else
    ui_ok 'Made keys/machine.key'
    ui_note "Control node and compute must hold the same one: copy this file to the other machine's keys/ (replacing the one its installer made), readable by its owner only."
  fi
fi
if [[ ! -f $ROOT/keys/weightroom.key ]] && (has_service weightroom || has_service turnstile); then
  if ((DRY_RUN)); then
    ui_note "DRY RUN: add the weightroom account and write keys/weightroom.key"
  else
    weightroom_cli=("$ROOT/.venv/bin/weightroom" --config "$ROOT/settings/weightroom.toml")
    # Its own account, and the key the console signs in with; made again by hand if this goes wrong.
    remake_key="$(shell_path "$ROOT/.venv/bin/weightroom") --config $(shell_path "$ROOT/settings/weightroom.toml") user key weightroom installer | grep -oE 'lc_[A-Za-z0-9_-]+' > $(shell_path "$ROOT/keys/weightroom.key") && chmod 600 $(shell_path "$ROOT/keys/weightroom.key")"
    added=$("${weightroom_cli[@]}" user add weightroom system 2>&1) ||
      die "weightroom could not add its own account (it said: ${added:-nothing})." \
        "If that says the account is there already, make it a new key and keep it: $remake_key" \
        "Otherwise put right what it says"
    (umask 077 && printf '%s\n' "$added" | grep -oE 'lc_[A-Za-z0-9_-]+' | tail -1 > "$ROOT/keys/weightroom.key") || true
    [[ -s $ROOT/keys/weightroom.key ]] || die 'The weightroom account was made, but no key could be read from what weightroom printed.' \
      "Make it a new key and keep it: $remake_key"
    "${weightroom_cli[@]}" user adjust weightroom add=sign-in,mirror-users >/dev/null
    ui_ok 'Made the weightroom account and keys/weightroom.key'
    if ((!EMBEDDED)); then
      ui_note "Add yourself with: .venv/bin/weightroom user add NAME superuser && .venv/bin/weightroom user password NAME"
      ui_note "or bring existing accounts with scripts/import-accounts.sh."
    fi
  fi
fi

if has_service voice || has_service voice-gateway; then
  ui__log_lines; VOICE_LOG_FROM=$REPLY
  try_step 'Preparing voice accounts and speech models' "$PY" "$ROOT/scripts/install-suite-support.py" voice --root "$ROOT" --services "$SELECTED_CSV" ||
    die 'The voice accounts and speech models could not be prepared.' \
      "The lines above, from the log, say why: put that right" \
      '?Speech models come from GitHub releases: check this machine can reach them: curl -sSI https://github.com | head -1' \
      'Or set up without voice: leave voice and voice-gateway out of --services'
  # A speech bundle changed upstream is installed anyway; say so on screen, not only in the log.
  if [[ -f ${OWG_LOG:-} ]]; then
    while IFS= read -r line; do ui_warn "$line"; done < <(tail -n "+$((VOICE_LOG_FROM + 1))" "$OWG_LOG" | sed -n 's/^WARN //p')
  fi
fi

if has_service loadcoach && [[ $STRATA_ACTION == install ]]; then
  if ! run "$PY" "$ROOT/engines/strata/install.py" --root "$ROOT" --host-ram-mib "$STRATA_HOST_RAM_MIB" --unit-dir "$UNIT_DIR"; then
    die 'Strata did not install. Existing engine settings were kept until verification succeeded.' \
      'Put right the build or memory-limit error above, then run owg install --strata install'
  fi
fi
if has_service loadcoach && [[ $SDCPP_ACTION == install ]]; then
  if ! run "$PY" "$ROOT/scripts/install-sdcpp.py" --root "$ROOT" --backend "$SDCPP_BACKEND"; then
    die "The image runtime (sdcpp) did not install. openWeightGym itself is set up, and any working release was kept." \
      "The lines above, from the log, say why: put that right" \
      "Then install it on its own: $(shell_path "$ROOT/scripts/install-sdcpp.sh") --root $(shell_path "$ROOT") --backend $SDCPP_BACKEND" \
      "Or set up without it: leave out --sdcpp install, or pass --sdcpp skip"
  fi
fi
if has_service loadcoach && [[ $AUDIOCPP_ACTION == install ]]; then
  if ! run "$PY" "$ROOT/soundroom/install.py" --root "$ROOT" --backend "$AUDIOCPP_BACKEND"; then
    die "The audio runtime (audio.cpp) did not install. openWeightGym itself is set up, and any working release was kept." \
      "The lines above, from the log, say why: put that right" \
      "Then install it on its own: $(shell_path "$ROOT/scripts/install-audiocpp.sh") --root $(shell_path "$ROOT") --backend $AUDIOCPP_BACKEND" \
      "Or set up without it: leave out --audiocpp install, or pass --audiocpp skip"
  fi
fi
if has_service loadcoach && [[ $WHISPERCPP_ACTION == install ]]; then
  if ! run "$PY" "$ROOT/engines/whisper_cpp/install.py" --root "$ROOT" --backend "$WHISPERCPP_BACKEND"; then
    die "The audio runtime (whisper.cpp) did not install. openWeightGym itself is set up, and any working release was kept." \
      "The lines above, from the log, say why: put that right" \
      "Then install it on its own: $(shell_path "$ROOT/scripts/install-whispercpp.sh") --root $(shell_path "$ROOT") --backend $WHISPERCPP_BACKEND" \
      "Or set up without it: leave out --whispercpp install, or pass --whispercpp skip"
  fi
fi

if has_service loadcoach && [[ $CLASSIFIER_TOOLS_ACTION == install ]]; then
  if ! run "$PY" "$ROOT/scripts/install-classifier-tools.py" --root "$ROOT"; then
    die 'Classifier conversion tools did not install. Existing settings and model files are kept.' \
      'Fix the tool log or configured llama.cpp error above, then retry: owg install --classifier-tools install'
  fi
fi

# Save versions after optional engines and tools are installed, before services start reading them.
try_step 'Recording component versions' "$PY" -m loadcoach.component_versions --root "$ROOT" ||
  die 'Component versions could not be saved to the database.' \
    'Put right the database or settings error above, then run owg repair'

render_unit() {
  local service=$1
  local source=$ROOT/services/$service.service target=$UNIT_DIR/$service.service
  if ((${KEEP_EXISTING_UNITS:-0})) && [[ -f $target ]]; then return 0; fi
  [[ -f $source ]] || die "The service file $(shell_path "$source") is missing: this copy of openWeightGym is incomplete." \
    "Put the file back: git -C $(shell_path "$ROOT") checkout -- services/$service.service" "Or repair the installation: owg repair"
  if ((DRY_RUN)); then
    ui_note "DRY RUN: render $source to $target with install root $ROOT"
  else
    "$PY" - "$source" "$target" "$ROOT" <<'PY'
from pathlib import Path
import json
import os
import shlex
import stat
import sys
import tempfile
import time
source, target, root = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
def replace_root(value):
    return value.replace('%h/ai/openWeightGym', root).replace('%h/ai/suite', root).replace('%h/weightroom', root)
def unit_argument(value):
    return json.dumps(value, ensure_ascii=False) if any(c.isspace() or c in '\\"' for c in value) else value
lines = []
for line in source.read_text().splitlines(keepends=True):
    if line.startswith('ExecStart='):
        arguments = shlex.split(line.split('=', 1)[1])
        line = 'ExecStart=' + ' '.join(unit_argument(replace_root(arg)) for arg in arguments) + '\n'
    elif line.startswith('EnvironmentFile='):
        key, value = line.rstrip('\n').split('=', 1)
        line = key + '=' + unit_argument(replace_root(value)) + '\n'
    else:
        line = replace_root(line)
    lines.append(line)
text = ''.join(lines)
masked = target.is_symlink() and os.readlink(target) == '/dev/null'
if target.exists() and not masked:
    if not target.is_symlink() and target.read_text() == text:
        raise SystemExit(0)
    backup = str(target) + f'.before-suite-install-{time.time_ns()}'
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        output.write(target.read_bytes())
mode = stat.S_IMODE(target.stat().st_mode) if target.exists() and not target.is_symlink() else 0o644
with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, prefix='.' + target.name, delete=False) as output:
    pending = Path(output.name)
    try:
        output.write(text)
        output.flush()
        os.fchmod(output.fileno(), mode)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)
PY
  fi
}

for service in "${SELECTED[@]}"; do
  render_unit "$service" || die "The service file for $service could not be written to $(shell_path "$UNIT_DIR")." \
    "The lines above say why" "Check the folder is yours and has room: ls -ld $(shell_path "$UNIT_DIR") && df -h $(shell_path "$UNIT_DIR")"
done
ui_ok "Service units in $UNIT_DIR"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  run systemctl --user daemon-reload ||
    die "systemd would not read the new service files." "Its message is just above; see what it makes of them: systemd-analyze --user verify $(shell_path "$UNIT_DIR")/*.service"
  for service in "${SELECTED[@]}"; do
    { run systemctl --user unmask -q "$service.service" && run systemctl --user enable -q "$service.service"; } ||
      die "systemd would not switch on $service." "Its message is just above; see what it makes of the unit: systemd-analyze --user verify $(shell_path "$UNIT_DIR/$service.service")"
    ((START_SERVICES)) || continue
    config_name=$(config_for "$service")
    if [[ ${WAS_CONFIGURED[$config_name]:-1} == 0 ]] && ((!START_NEW)); then
      ui_attention "$service is installed but not started: its settings are new." \
        "Look them over: $(shell_path "$ROOT/settings/$config_name.toml")" "Then start it: systemctl --user start $service"
    elif ! try_step "Starting $service" systemctl --user restart "$service.service"; then
      # What the program itself said, in the log, where the panel can find why it did not start.
      journalctl --user -u "$service.service" -n 30 --no-pager >>"${OWG_LOG:-/dev/null}" 2>&1 || true
      die "$service did not start." \
        "See what it said: journalctl --user -u $service -n 50 --no-pager" \
        "Its settings are in $(shell_path "$ROOT/settings/$config_name.toml")" \
        "Once that is put right, start it: systemctl --user restart $service"
    fi
  done
else
  ui_attention "systemd is not running for this account, so the services were installed but not switched on." \
    "Log in as $(id -un) directly, or over SSH, rather than switching to it with su: its systemd starts then" \
    "Or keep it running without a log-in: sudo loginctl enable-linger $(id -un)" \
    "Then switch the services on: systemctl --user daemon-reload && systemctl --user enable --now ${SELECTED[*]/%/.service}"
fi

if ((!EMBEDDED)); then
  if ((!DRY_RUN)); then
    ui_section 'Firewall'
    firewall_check "$ROOT" "$SELECTED_CSV" "$PY"
  fi
  ui_section 'Done' 'Installation complete.'
  ui_attention_box
  ui_note "Run '$ROOT/scripts/install-suite.sh --check --services $SELECTED_CSV' to repeat the compatibility audit."
fi
if [[ $DATABASE == postgresql ]]; then
  ui_note 'PostgreSQL credentials are referenced only through owner-only DSN files.'
  ((INSTALL_PGVECTOR)) && ui_note 'pgvector is installed; CREATE EXTENSION vector is intentionally deferred until an openWeightGym schema uses it.'
fi
exit 0
