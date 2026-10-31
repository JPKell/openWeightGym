#!/usr/bin/env bash
# Install selected openWeightGym programs on this machine and configure their database stores.
# Existing units, settings, and databases are discovered and checked before changes are made.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib/ui.sh
source "$SCRIPT_DIR/lib/ui.sh"
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
SERVICES=(loadcoach toollocker spotter voice weightroom turnstile voice-gateway)
COMPUTE=(loadcoach toollocker spotter voice)
FRONT=(weightroom turnstile voice-gateway)

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
AUDIOCPP_BACKEND=cuda
AUDIOCPP_EXPLICIT=0
TOPOLOGY=keep
ADDRESS=
FRONT_URL=
COMPUTE_URL=
LLAMA_SERVER=
START_NEW=0
UV=
EMBEDDED=0
declare -A DSNS=()
declare -A WAS_CONFIGURED=()
declare -A CONFIG_BACKUPS=()
declare -A EXISTING_INSTALL_SERVICES=()

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
  --dsn NAME=FILE            PostgreSQL DSN file (core, telemetry, accounts, chat,
                             benchmarks, plugins, alerts, or schedules); repeat as needed
  --install-postgresql       install PostgreSQL server and client packages
  --provision-postgresql     create local openWeightGym roles/databases and DSN files
  --pgvector                 install the pgvector package (implies PostgreSQL)
  --sdcpp install|skip|keep   optional image runtime (fresh unattended: skip)
  --sdcpp-backend cuda|vulkan|cpu
                             explicit native backend; default cuda, no CPU fallback
  --audiocpp install|skip|keep  optional audio runtime (fresh unattended: skip)
  --audiocpp-backend cuda|vulkan|cpu
                             explicit native backend; default cuda, no CPU fallback
  --no-start                 install and enable units without starting them
  --topology all|front|compute
                             write new settings for this machine's part: everything on
                             one machine, the console only, or a GPU node (default: keep
                             the examples' placeholders for you to edit)
  --address IP               this machine's address on its network (GPU node settings)
  --front-url URL            the console's turnstile, such as http://192.168.1.10:8442
  --compute-url URL          the GPU node's LoadCoach, such as http://192.168.1.20:8442
  --llama-server PATH        the llama-server LoadCoach runs (see install-llamacpp.sh)
  --start-new                also start services whose settings were just written
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

die() { ui_die "install-suite: $*" "" 2; }
note() { printf '%s\n' "$*"; }
quote_command() { printf ' %q' "$@"; printf '\n'; }
run() {
  if ((DRY_RUN)); then
    printf '  %sDRY RUN:%s' "$C_MUTED" "$C_RESET"
    quote_command "$@"
  else
    "$@"
  fi
}
# A longer piece of work, with a spinner and its output in the log.
step() {
  local label=$1
  shift
  if ((DRY_RUN)); then run "$@"; return 0; fi
  ui_step "$label" -- "$@" || die "$label failed. Fix what the log says and run this again; nothing finished is undone."
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
    --services) (($# >= 2)) || die '--services needs a value'; SERVICE_ARGUMENT=$2; shift 2 ;;
    --preset) (($# >= 2)) || die '--preset needs a value'; PRESET=$2; shift 2 ;;
    --database) (($# >= 2)) || die '--database needs a value'; DATABASE=$2; shift 2 ;;
    --dsn)
      (($# >= 2)) || die '--dsn needs NAME=FILE'
      [[ $2 == *=* ]] || die '--dsn needs NAME=FILE'
      DSNS[${2%%=*}]=${2#*=}; shift 2 ;;
    --install-postgresql) INSTALL_POSTGRESQL=1; shift ;;
    --provision-postgresql) PROVISION_POSTGRESQL=1; INSTALL_POSTGRESQL=1; shift ;;
    --pgvector) INSTALL_PGVECTOR=1; INSTALL_POSTGRESQL=1; shift ;;
    --sdcpp) (($# >= 2)) || die '--sdcpp needs install, skip or keep'; SDCPP_ACTION=$2; SDCPP_EXPLICIT=1; shift 2 ;;
    --sdcpp-backend) (($# >= 2)) || die '--sdcpp-backend needs cuda, vulkan or cpu'; SDCPP_BACKEND=$2; shift 2 ;;
    --audiocpp) (($# >= 2)) || die '--audiocpp needs install, skip or keep'; AUDIOCPP_ACTION=$2; AUDIOCPP_EXPLICIT=1; shift 2 ;;
    --audiocpp-backend) (($# >= 2)) || die '--audiocpp-backend needs cuda, vulkan or cpu'; AUDIOCPP_BACKEND=$2; shift 2 ;;
    --no-start) START_SERVICES=0; shift ;;
    --topology) (($# >= 2)) || die '--topology needs all, front or compute'; TOPOLOGY=$2; shift 2 ;;
    --address) (($# >= 2)) || die '--address needs an IP address'; ADDRESS=$2; shift 2 ;;
    --front-url) (($# >= 2)) || die '--front-url needs a URL'; FRONT_URL=$2; shift 2 ;;
    --compute-url) (($# >= 2)) || die '--compute-url needs a URL'; COMPUTE_URL=$2; shift 2 ;;
    --llama-server) (($# >= 2)) || die '--llama-server needs a path'; LLAMA_SERVER=$2; shift 2 ;;
    --start-new) START_NEW=1; shift ;;
    --uv) (($# >= 2)) || die '--uv needs a path'; UV=$2; shift 2 ;;
    --embedded) EMBEDDED=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --root) (($# >= 2)) || die '--root needs a path'; ROOT=$(cd -- "$2" && pwd); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

ui_init
: "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/install-suite-$(date +%Y%m%d-%H%M%S).log}"
mkdir -p "$(dirname "$OWG_LOG")" 2>/dev/null || OWG_LOG=/dev/null
export OWG_LOG

[[ -f $ROOT/pyproject.toml && -f $ROOT/scripts/install-suite-support.py ]] || die "$ROOT is not an openWeightGym checkout"
[[ $TOPOLOGY =~ ^(keep|all|front|compute)$ ]] || die 'topology must be all, front, or compute'
if [[ $TOPOLOGY == compute && -z $FRONT_URL ]]; then die "a GPU node needs --front-url, the console machine's turnstile address"; fi
[[ -z $FRONT_URL || $FRONT_URL =~ ^https?://[^/[:space:]]+/?$ ]] || die '--front-url must look like http://192.168.1.10:8442'
[[ -z $COMPUTE_URL || $COMPUTE_URL =~ ^https?://[^/[:space:]]+/?$ ]] || die '--compute-url must look like http://192.168.1.20:8442'
[[ -z $UV || -x $UV ]] || die "--uv $UV is not a program"
if [[ $TOPOLOGY != keep && -z $ADDRESS ]]; then
  ADDRESS=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}') || true
  ADDRESS=${ADDRESS:-127.0.0.1}
fi
[[ $DATABASE =~ ^(auto|keep|sqlite|postgresql)$ ]] || die 'database must be keep, sqlite, or postgresql'
[[ -z $PRESET || $PRESET =~ ^(compute|front|all)$ ]] || die 'preset must be compute, front, or all'
[[ $SDCPP_ACTION =~ ^(install|skip|keep)$ ]] || die 'sdcpp must be install, skip, or keep'
[[ $SDCPP_BACKEND =~ ^(cuda|vulkan|cpu)$ ]] || die 'sdcpp backend must be cuda, vulkan, or cpu'
[[ $AUDIOCPP_ACTION =~ ^(install|skip|keep)$ ]] || die 'audiocpp must be install, skip, or keep'
[[ $AUDIOCPP_BACKEND =~ ^(cuda|vulkan|cpu)$ ]] || die 'audiocpp backend must be cuda, vulkan, or cpu'
for name in "${!DSNS[@]}"; do
  [[ $name =~ ^(core|telemetry|accounts|chat|benchmarks|plugins|alerts|schedules)$ ]] || die "unknown DSN name: $name"
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
  valid_service "$1" || die "unknown service: $1"
  has_service "$1" || SELECTED+=("$1")
}

parse_services() {
  local value=${1// /} item
  SELECTED=()
  IFS=',' read -ra requested <<< "$value"
  for item in "${requested[@]}"; do
    case "$item" in
      compute) for item in "${COMPUTE[@]}"; do add_service "$item"; done ;;
      front) for item in "${FRONT[@]}"; do add_service "$item"; done ;;
      all) for item in "${SERVICES[@]}"; do add_service "$item"; done ;;
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
  die 'no installed openWeightGym services found; use --services or --preset'
else
  ui_info 'No installed openWeightGym service units were found.'
  choice=0
  ui_choose choice 'Which programs should run on this machine?' 0 \
    'All of them|the console and a GPU node on this one machine' \
    'The console|weightroom, turnstile and voice-gateway' \
    'A GPU node|loadcoach, toollocker, spotter and voice'
  PRESETS=(all front compute)
  parse_services "${PRESETS[choice]}"
fi

((${#SELECTED[@]})) || die 'select at least one service'

if ((!NON_INTERACTIVE && ${#DETECTED[@]})); then
  ui_info "Detected and preselected: $(join_by_comma "${DETECTED[@]}")"
fi
if ((!NON_INTERACTIVE)); then
  declare -A ABOUT=(
    [loadcoach]='runs the models (GPU node)' [toollocker]='tools for models over MCP (GPU node)'
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
  ((${#SELECTED[@]})) || die 'select at least one service'
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
    executable=${expanded_command%% *}
    existing_root=
    if [[ $executable == */.venv/bin/* ]]; then
      existing_root=${executable%%/.venv/bin/*}
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
if [[ -x $ROOT/.venv/bin/python ]]; then
  if "$ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 13))'; then
    ui_ok "virtual environment: $($ROOT/.venv/bin/python --version 2>&1)"
    CHECK_PYTHON=$ROOT/.venv/bin/python
  else
    die "$ROOT/.venv uses Python older than 3.13 and is not compatible"
  fi
else
  ui_ok 'virtual environment: new environment will be created'
  CHECK_PYTHON=python3
  # With uv the system may have no Python of its own: use the one uv keeps.
  if [[ -n $UV ]]; then CHECK_PYTHON=$("$UV" python find 3.13 2>/dev/null) || die 'uv has no Python 3.13; run: uv python install 3.13'; fi
fi
PY=$CHECK_PYTHON
ui_info 'Existing-install compatibility check'
initial_audit=(--root "$ROOT" --services "$SELECTED_CSV")
((CHECK_ONLY)) && initial_audit+=(--require-pg-tools)
if ! audit "$CHECK_PYTHON" "${initial_audit[@]}"; then
  die 'the existing installation is not compatible; no changes were made'
fi
for existing_root in "${!EXISTING_INSTALL_SERVICES[@]}"; do
  [[ $existing_root == "$ROOT" ]] && continue
  [[ -d $existing_root ]] || die "selected services point at missing installation $existing_root"
  ui_info "Compatibility check for existing installation at $existing_root"
  if ! audit "$CHECK_PYTHON" \
      --root "$existing_root" --services "${EXISTING_INSTALL_SERVICES[$existing_root]}"; then
    die "the existing installation at $existing_root is not compatible; no changes were made"
  fi
  die "selected services already run from $existing_root; run that checkout's installer or move its settings, keys, and data explicitly before repointing units"
done

if ((CHECK_ONLY)); then
  ui_info "Installed openWeightGym units: ${DETECTED[*]:-(none)}"
  ui_info "Selected openWeightGym units: ${SELECTED[*]}"
  has_service loadcoach && "$PY" "$ROOT/scripts/install-sdcpp.py" --root "$ROOT" --check
  has_service loadcoach && "$PY" "$ROOT/soundroom/install.py" --root "$ROOT" --check
  exit 0
fi

if has_service loadcoach; then
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
elif [[ $SDCPP_ACTION == install ]]; then
  die 'sdcpp belongs on a compute host; select loadcoach or install it later on that host'
elif [[ $AUDIOCPP_ACTION == install ]]; then
  die 'audio.cpp belongs on a compute host; select loadcoach or install it later on that host'
fi

read -r CURRENT_DATABASE DATABASE_CONTENT <<< "$("$CHECK_PYTHON" "$ROOT/scripts/install-suite-support.py" state --root "$ROOT" --services "$SELECTED_CSV")"
if [[ $DATABASE == auto ]]; then
  if ((NON_INTERACTIVE)); then DATABASE=keep; else
    ui_choose choice 'Where should openWeightGym keep its data?' 0 \
      "Keep current|$CURRENT_DATABASE" 'SQLite|files in data/, nothing else to run' 'PostgreSQL|a database server, local or elsewhere'
    DATABASES=(keep sqlite postgresql)
    DATABASE=${DATABASES[choice]}
  fi
fi
[[ $DATABASE =~ ^(keep|sqlite|postgresql)$ ]] || die 'database must be keep, sqlite, or postgresql'
if [[ $DATABASE != keep && $DATABASE != "$CURRENT_DATABASE" && $DATABASE_CONTENT == populated ]]; then
  die "selected stores contain data in $CURRENT_DATABASE; this installer will not switch engines without a verified migration"
fi
if [[ $DATABASE != postgresql && (${#DSNS[@]} -gt 0 || $PROVISION_POSTGRESQL == 1) ]]; then
  die 'PostgreSQL DSNs and provisioning require --database postgresql'
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

REQUIRED_DSNS=()
if has_service loadcoach || has_service spotter || has_service toollocker; then REQUIRED_DSNS+=(core); fi
has_service spotter && REQUIRED_DSNS+=(telemetry)
if has_service weightroom; then REQUIRED_DSNS+=(accounts chat benchmarks plugins alerts schedules); elif has_service turnstile; then REQUIRED_DSNS+=(accounts chat benchmarks plugins); fi

install_postgresql_packages() {
  ((INSTALL_POSTGRESQL)) || return 0
  if command -v apt-get >/dev/null; then
    run sudo apt-get update
    run sudo apt-get install -y postgresql postgresql-client
  elif command -v dnf >/dev/null; then
    run sudo dnf install -y postgresql-server postgresql
  elif command -v pacman >/dev/null; then
    run sudo pacman -S --needed postgresql
  else
    die 'automatic PostgreSQL installation supports apt, dnf, and pacman; install server/client packages manually'
  fi
  if ((PROVISION_POSTGRESQL && !DRY_RUN)) && command -v systemctl >/dev/null; then
    if command -v postgresql-setup >/dev/null && [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then
      sudo postgresql-setup --initdb
    fi
    sudo systemctl enable --now postgresql
  fi
}

install_pgvector_package() {
  ((INSTALL_PGVECTOR)) || return 0
  local package= major=
  if command -v apt-get >/dev/null; then
    if command -v pg_config >/dev/null; then major=$(pg_config --version | sed -E 's/.* ([0-9]+).*/\1/'); fi
    if [[ -n $major ]] && apt-cache show "postgresql-$major-pgvector" >/dev/null 2>&1; then
      package="postgresql-$major-pgvector"
    elif apt-cache show postgresql-pgvector >/dev/null 2>&1; then
      package=postgresql-pgvector
    else
      package=$(apt-cache pkgnames 2>/dev/null | grep -E '^postgresql-[0-9]+-pgvector$' | sort -V | tail -1 || true)
    fi
    [[ -n $package ]] || die 'no pgvector package is available from the configured apt repositories'
    run sudo apt-get install -y "$package"
  elif command -v dnf >/dev/null; then
    dnf list --available pgvector >/dev/null 2>&1 || die 'the configured dnf repositories do not provide pgvector'
    run sudo dnf install -y pgvector
  elif command -v pacman >/dev/null; then
    run sudo pacman -S --needed pgvector
  else
    die 'automatic pgvector installation supports apt, dnf, and pacman'
  fi
}

provision_postgresql() {
  ((PROVISION_POSTGRESQL)) || return 0
  ((DRY_RUN)) && { note 'DRY RUN: provision local PostgreSQL role, separate databases, and mode-600 DSN files'; return 0; }
  command -v psql >/dev/null && command -v createdb >/dev/null || die 'psql and createdb were not installed'
  local host role prefix password key database dsn_dir existing
  host=$(hostname -s | tr '[:upper:]-.' '[:lower:]__' | tr -cd 'a-z0-9_')
  role="suite_${USER//[^a-zA-Z0-9_]/_}_${host:-host}"
  role=${role:0:60}
  prefix=${SUITE_DATABASE_PREFIX:-${role}}
  dsn_dir=$ROOT/keys/postgresql
  install -d -m 700 "$dsn_dir"
  existing=$(sudo -u postgres psql -Atqc "SELECT 1 FROM pg_roles WHERE rolname = '$role'" postgres)
  if [[ $existing == 1 ]]; then
    local complete=1 suffix=2
    for key in "${REQUIRED_DSNS[@]}"; do
      [[ -f $dsn_dir/$key.dsn ]] || complete=0
    done
    if ((complete)); then
      for key in "${REQUIRED_DSNS[@]}"; do DSNS[$key]=$dsn_dir/$key.dsn; done
      note "Using existing local PostgreSQL role and DSN files for $role."
      return 0
    fi
    while sudo -u postgres psql -Atqc "SELECT 1 FROM pg_roles WHERE rolname = '${role}_$suffix'" postgres | grep -q 1; do
      ((suffix++))
    done
    role="${role}_$suffix"
    prefix=$role
  fi
  password=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')
  printf 'CREATE ROLE "%s" LOGIN PASSWORD '\''%s'\'';\n' "$role" "$password" |
    sudo -u postgres psql -v ON_ERROR_STOP=1 -q postgres
  for key in "${REQUIRED_DSNS[@]}"; do
    database="${prefix}_${key}"; database=${database:0:60}
    sudo -u postgres createdb --owner "$role" "$database"
    printf 'postgresql://%s:%s@127.0.0.1:5432/%s\n' "$role" "$password" "$database" > "$dsn_dir/$key.dsn"
    chmod 600 "$dsn_dir/$key.dsn"
    DSNS[$key]=$dsn_dir/$key.dsn
  done
  unset password
}

collect_dsns() {
  [[ $DATABASE == postgresql ]] || return 0
  local key value
  for key in "${REQUIRED_DSNS[@]}"; do
    [[ -n ${DSNS[$key]:-} ]] && continue
    if ((DRY_RUN && PROVISION_POSTGRESQL)); then
      DSNS[$key]=$ROOT/keys/postgresql/$key.dsn
    elif ((NON_INTERACTIVE)); then
      die "PostgreSQL needs --dsn $key=FILE (or --provision-postgresql)"
    else
      read -r -p "Owner-only PostgreSQL DSN file for $key: " value
      [[ -n $value ]] || die "a DSN file is required for $key"
      DSNS[$key]=$value
    fi
  done
  for key in "${REQUIRED_DSNS[@]}"; do
    DSNS[$key]=$(realpath -m -- "${DSNS[$key]}")
    if ((!DRY_RUN)); then
      [[ -f ${DSNS[$key]} ]] || die "DSN file does not exist: ${DSNS[$key]}"
      [[ -z $(find "${DSNS[$key]}" -maxdepth 0 -perm /077 -print) ]] || die "DSN file must be owner-only (mode 600): ${DSNS[$key]}"
    fi
  done
}

if ((!EMBEDDED)); then
  ui_section 'Game plan' 'Installation choices'
  ui_kv Source "$ROOT"
  ui_kv Services "${SELECTED[*]}"
  ui_kv Database "$DATABASE"
  [[ $TOPOLOGY != keep ]] && ui_kv 'This machine' "$TOPOLOGY ($ADDRESS)"
  has_service loadcoach && ui_kv Images "sdcpp $SDCPP_ACTION (backend: $SDCPP_BACKEND; weights downloaded separately)"
  has_service loadcoach && ui_kv Audio "audio.cpp $AUDIOCPP_ACTION (backend: $AUDIOCPP_BACKEND; weights downloaded separately)"
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
    die "$ROOT/.venv uses Python older than 3.13; replace that environment before installing"
  fi
elif [[ -n $UV ]]; then
  step 'Making the Python environment' "$UV" venv --seed --python 3.13 "$ROOT/.venv"
else
  step 'Making the Python environment' python3 -m venv "$ROOT/.venv"
fi
((DRY_RUN)) || PY=$ROOT/.venv/bin/python
if [[ -n $UV ]]; then
  pip_install() { "$UV" pip install --python "$ROOT/.venv/bin/python" "$@"; }
else
  pip_install() { "$ROOT/.venv/bin/python" -m pip install "$@"; }
fi
step 'Bringing pip up to date' pip_install --upgrade 'pip>=26.2.1'

extras=()
((USES_POSTGRESQL)) && extras+=(postgresql)
(has_service weightroom || has_service turnstile) && extras+=(benchmarks)
has_service weightroom && extras+=(knowledge)
has_service voice && extras+=(voice)
has_service voice-gateway && extras+=(voice-gateway)
extra_csv=$(join_by_comma "${extras[@]}")
if [[ -n $extra_csv ]]; then
  step "Installing Python packages ($extra_csv)" pip_install -e "${ROOT}[$extra_csv]"
else
  step 'Installing Python packages' pip_install -e "$ROOT"
fi
step 'Compiling' "$ROOT/.venv/bin/python" -m compileall -q "$ROOT/engines" "$ROOT/mirrorwall" "$ROOT/soundroom" "$ROOT/db" "$ROOT/loadcoach" "$ROOT/toollocker" "$ROOT/spotter" "$ROOT/weightroom" "$ROOT/turnstile" "$ROOT/voice"

mkdir_if_real() { if ((DRY_RUN)); then ui_note "DRY RUN: mkdir -p $*"; else mkdir -p "$@"; fi; }
mkdir_if_real "$ROOT/settings/defaults" "$ROOT/settings/examples" "$ROOT/data" "$ROOT/keys" "$UNIT_DIR"
((DRY_RUN)) || chmod 700 "$ROOT/keys"
# The folder the files tool reads, and the models folder LoadCoach is given, for new settings.
if [[ $TOPOLOGY != keep ]]; then mkdir_if_real "$ROOT/data/shared" "$ROOT/models"; fi

config_for() {
  case "$1" in turnstile) printf 'weightroom';; *) printf '%s' "$1";; esac
}

install_example_config() {
  local config_name=$1 source_config target_config
  source_config=$ROOT/settings/examples/$config_name.toml
  target_config=$ROOT/settings/$config_name.toml
  [[ -f $source_config ]] || die "missing example configuration: $source_config"
  if [[ ! -f $target_config ]]; then
    [[ -v WAS_CONFIGURED[$config_name] ]] || WAS_CONFIGURED[$config_name]=0
    if ((DRY_RUN)); then
      ui_note "DRY RUN: render $source_config to $target_config for $ROOT and $HOME ($TOPOLOGY)"
    else
      "$PY" "$ROOT/scripts/install-suite-support.py" render --example "$source_config" --target "$target_config" \
        --root "$ROOT" --home "$HOME" --topology "$TOPOLOGY" --address "${ADDRESS:-127.0.0.1}" \
        --front "$FRONT_URL" --compute "$COMPUTE_URL" --llama-server "$LLAMA_SERVER" ||
        die "could not write $target_config"
      ui_ok "Wrote settings/$config_name.toml"
    fi
  else
    [[ -v WAS_CONFIGURED[$config_name] ]] || WAS_CONFIGURED[$config_name]=1
  fi
}

for service in "${SELECTED[@]}"; do
  config_name=$(config_for "$service")
  [[ $config_name == voice-gateway || $config_name == voice || $config_name == loadcoach || $config_name == spotter || $config_name == toollocker || $config_name == weightroom ]] || continue
  install_example_config "$config_name"
done
if (has_service spotter || has_service toollocker) && [[ ! -f $ROOT/settings/loadcoach.toml ]]; then
  ui_info 'Installing LoadCoach settings as the shared identity configuration for Spotter/ToolLocker.'
  install_example_config loadcoach
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
  for service in loadcoach spotter toollocker weightroom; do
    if ! has_service "$service"; then
      [[ $service == weightroom ]] && has_service turnstile || continue
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
    "$PY" "$ROOT/scripts/install-suite-support.py" "${arguments[@]}"
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
    die 'new database settings failed compatibility checks; original settings were restored'
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
    ui_note "Front and compute must hold the same one: copy this file to the other machine's keys/ (replacing the one its installer made), readable by its owner only."
  fi
fi
if [[ ! -f $ROOT/keys/weightroom.key ]] && (has_service weightroom || has_service turnstile); then
  if ((DRY_RUN)); then
    ui_note "DRY RUN: add the weightroom account and write keys/weightroom.key"
  else
    weightroom_cli=("$ROOT/.venv/bin/weightroom" --config "$ROOT/settings/weightroom.toml")
    added=$("${weightroom_cli[@]}" user add weightroom system)
    (umask 077 && printf '%s\n' "$added" | grep -oE 'lc_[A-Za-z0-9_-]+' | tail -1 > "$ROOT/keys/weightroom.key")
    [[ -s $ROOT/keys/weightroom.key ]] || die 'could not read the weightroom account key'
    "${weightroom_cli[@]}" user adjust weightroom add=sign-in,mirror-users >/dev/null
    ui_ok 'Made the weightroom account and keys/weightroom.key'
    if ((!EMBEDDED)); then
      ui_note "Add yourself with: .venv/bin/weightroom user add NAME superuser && .venv/bin/weightroom user password NAME"
      ui_note "or bring existing accounts with scripts/import-accounts.sh."
    fi
  fi
fi

if has_service loadcoach && [[ $SDCPP_ACTION == install ]]; then
  if ! run "$ROOT/scripts/install-sdcpp.sh" --root "$ROOT" --backend "$SDCPP_BACKEND"; then
    die 'openWeightGym packages/settings were installed, but optional sdcpp setup failed. Correct its requirements and rerun scripts/install-sdcpp.sh; existing working runtime releases were preserved.'
  fi
fi
if has_service loadcoach && [[ $AUDIOCPP_ACTION == install ]]; then
  if ! run "$ROOT/scripts/install-audiocpp.sh" --root "$ROOT" --backend "$AUDIOCPP_BACKEND"; then
    die 'openWeightGym packages/settings were installed, but optional audio.cpp setup failed. Correct its requirements and rerun scripts/install-audiocpp.sh; existing working runtime releases were preserved.'
  fi
fi

render_unit() {
  local service=$1
  local source=$ROOT/services/$service.service target=$UNIT_DIR/$service.service backup
  [[ -f $source ]] || die "missing service unit: $source"
  if [[ -L $target ]]; then
    if [[ $(readlink "$target") != /dev/null ]]; then
      backup=$target.before-suite-install-$(date -u +%Y%m%dT%H%M%SZ)
      run cp -L -p "$target" "$backup"
    fi
    run rm -f "$target"
  fi
  # A unit this installation rendered before is simply replaced; anyone else's is kept as a copy.
  if [[ -e $target && ! -L $target ]] && ! grep -q "^ExecStart=$ROOT/" "$target"; then
    backup=$target.before-suite-install-$(date -u +%Y%m%dT%H%M%SZ)
    run cp -p "$target" "$backup"
  fi
  if ((DRY_RUN)); then
    ui_note "DRY RUN: render $source to $target with install root $ROOT"
  else
    "$PY" - "$source" "$target" "$ROOT" <<'PY'
from pathlib import Path
import sys
source, target, root = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
text = source.read_text().replace('%h/ai/openWeightGym', root).replace('%h/ai/suite', root).replace('%h/weightroom', root)
target.write_text(text)
PY
  fi
}

for service in "${SELECTED[@]}"; do render_unit "$service"; done
ui_ok "Service units in $UNIT_DIR"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  run systemctl --user daemon-reload
  for service in "${SELECTED[@]}"; do
    run systemctl --user unmask -q "$service.service"
    run systemctl --user enable -q "$service.service"
    ((START_SERVICES)) || continue
    config_name=$(config_for "$service")
    if [[ ${WAS_CONFIGURED[$config_name]:-1} == 0 ]] && ((!START_NEW)); then
      ui_warn "$service installed but not started: review the new settings/$config_name.toml first."
    else
      step "Starting $service" systemctl --user restart "$service.service"
    fi
  done
else
  ui_warn 'systemd is not running for this account; units were installed but not enabled.'
fi

if ((!EMBEDDED)); then
  ui_section 'Done' 'Installation complete.'
  ui_note "Run '$ROOT/scripts/install-suite.sh --check --services $SELECTED_CSV' to repeat the compatibility audit."
fi
if [[ $DATABASE == postgresql ]]; then
  ui_note 'PostgreSQL credentials are referenced only through owner-only DSN files.'
  ((INSTALL_PGVECTOR)) && ui_note 'pgvector is installed; CREATE EXTENSION vector is intentionally deferred until an openWeightGym schema uses it.'
fi
exit 0
