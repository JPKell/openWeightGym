#!/usr/bin/env bash
# Install openWeightGym: your own models behind a web console, an OpenAI-compatible API, tools
# and voice. Run it either way:
#
#   curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh | bash
#   ./install.sh               from a checkout, which then becomes the installation
#
# It checks the machine, asks a few questions (Enter takes the defaults), then downloads what is
# needed, writes settings for this machine's part, starts the services and checks they answer.
# Running it again on an installed machine offers to update, repair or uninstall. `--help` lists
# options for unattended installs; with `curl | bash`, pass them after `bash -s --`.
#
# Two kinds of installation: for you (in ~/openWeightGym, as your own systemd user services), or
# for the whole machine (--system: code in /opt/openweightgym, keys in /etc/openweightgym, data
# and models in /var/lib/openweightgym, run by an openweightgym account that starts at boot).
#
# Everything happens inside main(), called on the last line: bash has read the whole file before
# anything runs, so a download cut short cannot run half an installer.

set -Eeuo pipefail

OWG_REPO=${OWG_REPO:-https://github.com/JPKell/openWeightGym.git}
OWG_RAW=${OWG_RAW:-https://raw.githubusercontent.com/JPKell/openWeightGym}
OWG_REF=${OWG_REF:-main}
OWG_VERSION=0.1.0
UV_VERSION=${OWG_UV_VERSION:-0.13.1}
# The starter model: small, open (Apache 2.0) and quick on any machine.
STARTER_NAME=qwen3-1.7b
STARTER_TITLE="Qwen3 1.7B"
STARTER_URL=https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf
STARTER_FILE=Qwen3-1.7B-Q8_0.gguf
STARTER_CONTEXT=8192
# Where a system installation goes, and the account it runs as.
SVC=${OWG_SERVICE_USER:-openweightgym}
SYS_OPT=${OWG_SYSTEM_OPT:-/opt/openweightgym}
SYS_ETC=${OWG_SYSTEM_ETC:-/etc/openweightgym}
SYS_VAR=${OWG_SYSTEM_VAR:-/var/lib/openweightgym}
SYS_BIN=${OWG_SYSTEM_BIN:-/usr/local/bin}

usage() {
  cat <<'EOF'
Usage: install.sh [options]        (curl ... | bash -s -- [options])

  --dir PATH              install here (default: ~/openWeightGym, or this checkout)
  --system                install for the whole machine, run by its own account (needs sudo)
  --topology NAME         all (one machine), front (console only) or compute (GPU node)
  --yes                   take the defaults without asking
  --admin NAME            the first account (its password from OWG_ADMIN_PASSWORD, or asked)
  --front-url URL         GPU node: the console's turnstile, such as http://192.168.1.10:8442
  --machine-key-file F    GPU node: a copy of the console's keys/machine.key
  --compute-url URL       console: the GPU node's LoadCoach, such as http://192.168.1.20:8442
  --join URL --code CODE  GPU node: join the console at URL (http://192.168.1.10:8440) with a
                          one-time code made there by `owg pair` or Settings → Compute nodes
  --llama CHOICE          auto, cuda-13.4, cuda-12.8, rocm, vulkan, cpu, source, skip, or the
                          path of a llama-server you already have (default: auto)
  --database NAME         sqlite (default) or postgresql (installed and set up locally)
  --voice                 also install speech recognition and voices
  --voice-key-file FILE   manual GPU-node setup: the console's voice-service.key (pairing supplies it)
  --power-control         let the console put this machine to sleep, restart or switch it off
  --no-model              skip the starter model
  --no-linger             let the services stop when you log out
  --no-browser            do not open the console in a browser at the end
  --ref BRANCH            which branch of openWeightGym to fetch (default: main)
  --plain                 no colour or animation
  --update                update an installed openWeightGym (as owg update does)
  --repair                reinstall packages and services as they are
  --uninstall             remove openWeightGym (see uninstall.sh --help)
  -h, --help              show this help

Environment: OWG_HOME (same as --dir), OWG_ADMIN_PASSWORD, NO_COLOR, OWG_QUIET=1 (no animation).
EOF
}

main() {
  if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
    echo "openWeightGym's installer needs bash 4.4 or newer; this is bash $BASH_VERSION." >&2
    exit 1
  fi
  parse_arguments "$@"
  load_ui
  ui_init
  : "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/install-$(date +%Y%m%d-%H%M%S).log}"
  ui_log_init || ui_die "Cannot create a private installer log at $OWG_LOG."
  export OWG_LOG
  trap 'ui_cleanup; on_error $LINENO' ERR
  trap 'ui_cleanup' EXIT
  trap 'ui_cleanup; printf "\n"; ui_fail "Stopped. Nothing finished is undone; run the installer again to carry on."; exit 130' INT

  locate_installation
  if [[ $ACTION == uninstall ]]; then run_uninstaller; fi
  ui_banner "$(installed_version)"
  warm_up
  if [[ $(receipt_get state) == pending ]]; then
    resume_settings
    ui_info "Resuming the confirmed installation plan" "settings and existing accounts are kept"
  elif is_installed "$DIR"; then
    ((SYSTEM)) && ui_sudo "looking after the system installation"
    case "$ACTION" in
      update) update ;;
      repair) reinstall ;;
      *) installed_menu ;;
    esac
    return 0
  else
    if [[ $ACTION == repair && -x $DIR/.venv/bin/python ]]; then ACTION=''; fi
    [[ -z $ACTION ]] || ui_die "openWeightGym is not installed in $DIR." "Install it first, or give its folder with --dir."
    # An older interrupted run has no checkpoint. Preserve its database selection on retry.
    if compgen -G "$DIR/settings/*.toml" >/dev/null && [[ -z ${PLAN_GIVEN[DATABASE]:-} ]]; then DATABASE=keep; fi
    game_plan
  fi
  account
  loading_plates
  setting_up
  finishing
  spotting
  write_receipt complete
  finale
}

on_error() {
  ((BASH_SUBSHELL == 0)) || return 0  # a failure inside $(...) or a step is reported where it is used
  ui_fail "Something went wrong (installer line $1)."
  ui_note "Nothing finished is undone; fix what the log says and run the installer again."
  ui_note "Log: $OWG_LOG"
}

# ---- arguments ------------------------------------------------------------------------------

parse_arguments() {
  DIR=${OWG_HOME:-} DIR_GIVEN=0 SYSTEM=0 TOPOLOGY='' ASSUME_YES=0 ADMIN='' FRONT_URL='' MACHINE_KEY_FILE=''
  COMPUTE_URL='' LLAMA=auto DATABASE=sqlite VOICE=0 STARTER=1 LINGER_WANTED=1 BROWSER=1 POWER_RULE=0
  ACTION='' PASSTHROUGH=() JOIN_URL='' PAIR_CODE='' PAIRED=0 MACHINE_NAME=''
  VOICE_KEY_FILE='' VOICE_KEY=''
  declare -gA PLAN_GIVEN=()
  [[ -n $DIR ]] && DIR_GIVEN=1
  while (($#)); do
    case "$1" in
      --dir) DIR=${2:?--dir needs a folder}; DIR_GIVEN=1; shift 2 ;;
      --system) SYSTEM=1; shift ;;
      --topology) TOPOLOGY=${2:?--topology needs all, front or compute}; PLAN_GIVEN[TOPOLOGY]=1; shift 2 ;;
      --yes|--express) ASSUME_YES=1; shift ;;
      --admin) ADMIN=${2:?--admin needs a name}; PLAN_GIVEN[ADMIN]=1; shift 2 ;;
      --front-url) FRONT_URL=${2:?--front-url needs a URL}; PLAN_GIVEN[FRONT_URL]=1; shift 2 ;;
      --machine-key-file) MACHINE_KEY_FILE=${2:?--machine-key-file needs a file}; shift 2 ;;
      --compute-url) COMPUTE_URL=${2:?--compute-url needs a URL}; PLAN_GIVEN[COMPUTE_URL]=1; shift 2 ;;
      --join) JOIN_URL=${2:?--join needs the console address}; TOPOLOGY=compute; PLAN_GIVEN[JOIN_URL]=1; PLAN_GIVEN[TOPOLOGY]=1; shift 2 ;;
      --code) PAIR_CODE=${2:?--code needs the pairing code}; shift 2 ;;
      --llama) LLAMA=${2:?--llama needs a choice}; PLAN_GIVEN[LLAMA]=1; shift 2 ;;
      --database) DATABASE=${2:?--database needs sqlite or postgresql}; PLAN_GIVEN[DATABASE]=1; shift 2 ;;
      --voice) VOICE=1; PLAN_GIVEN[VOICE]=1; shift ;;
      --voice-key-file) VOICE_KEY_FILE=${2:?--voice-key-file needs a file}; shift 2 ;;
      --power-control) POWER_RULE=1; PLAN_GIVEN[POWER_RULE]=1; shift ;;
      --no-model) STARTER=0; PLAN_GIVEN[STARTER]=1; shift ;;
      --no-linger) LINGER_WANTED=0; PLAN_GIVEN[LINGER_WANTED]=1; shift ;;
      --no-browser) BROWSER=0; shift ;;
      --ref) OWG_REF=${2:?--ref needs a branch}; shift 2 ;;
      --plain) export OWG_PLAIN=1; PASSTHROUGH+=(--plain); shift ;;
      --update) ACTION=update; shift ;;
      --repair) ACTION=repair; shift ;;
      --uninstall) ACTION=uninstall; shift ;;
      --everything|--no-backup) PASSTHROUGH+=("$1"); shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "install.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
  done
  [[ -z $TOPOLOGY || $TOPOLOGY =~ ^(all|front|compute)$ ]] || { echo "install.sh: --topology is all, front or compute" >&2; exit 2; }
  [[ $DATABASE =~ ^(sqlite|postgresql)$ ]] || { echo "install.sh: --database is sqlite or postgresql" >&2; exit 2; }
  if ((SYSTEM)) && [[ $DATABASE == postgresql ]]; then
    echo "install.sh: a system installation uses SQLite for now; set PostgreSQL up afterwards (docs/database-storage.md)" >&2
    exit 2
  fi
  ((ASSUME_YES)) && export OWG_DEFAULTS=1
  return 0
}

# The console's address as typed, made into its URL: 192.168.1.10 becomes http://192.168.1.10:8440.
console_url() {
  local url=$1
  [[ $url == http://* || $url == https://* ]] || url=http://$url
  url=${url%/}
  [[ ${url#*://} == *:* ]] || url=$url:8440
  printf '%s' "$url"
}

# The look, the machine checks and the receipt come from the checkout beside this file or, under
# `curl | bash`, from the same branch on GitHub.
load_ui() {
  CHECKOUT=''
  local self=${BASH_SOURCE[0]:-} part
  if [[ -n $self && -f $self && -f $(dirname "$self")/scripts/lib/ui.sh ]]; then
    CHECKOUT=$(cd -- "$(dirname -- "$self")" && pwd)
    for part in ui detect receipt; do
      # shellcheck disable=SC1090
      source "$CHECKOUT/scripts/lib/$part.sh"
    done
  else
    command -v curl >/dev/null || { echo "install.sh needs curl." >&2; exit 1; }
    BOOT=$(mktemp -d)
    for part in ui detect receipt; do
      curl -fsSL --retry 2 "$OWG_RAW/$OWG_REF/scripts/lib/$part.sh" -o "$BOOT/$part.sh" ||
        { echo "Could not fetch openWeightGym's installer from $OWG_RAW ($OWG_REF). Check the network and try again." >&2; exit 1; }
      # shellcheck disable=SC1090
      source "$BOOT/$part.sh"
    done
    rm -rf "$BOOT"
  fi
}

# Which installation this run is about: the one named, a system one already here, or the default
# place for the kind chosen. A system installation's receipt makes the rest of the run act as its account.
locate_installation() {
  if ((!DIR_GIVEN)); then
    if ((SYSTEM)); then DIR=$SYS_OPT
    else
      DIR=${CHECKOUT:-$HOME/openWeightGym}
      if ! receipt_find "$DIR" >/dev/null && ! is_installed "$DIR" && receipt_find "$SYS_OPT" >/dev/null; then DIR=$SYS_OPT; fi
    fi
  fi
  DIR=$(realpath -m -- "$DIR")
  receipt_load "$DIR"
  if [[ $(receipt_get mode) == system ]]; then
    SYSTEM=1
    SVC=$(receipt_get service_user)
    SYS_OPT=$DIR
    SYS_ETC=${RECEIPT[etc_dir]:-$SYS_ETC}
    SYS_VAR=${RECEIPT[var_dir]:-$SYS_VAR}
  fi
  system_mode_settings
}

# In a system installation the service account owns the files and runs the programs.
system_mode_settings() {
  if ((SYSTEM)); then
    OWG_OWNER=$SVC
    OWG_OWNER_LOG=$SYS_VAR/logs/$(basename "$OWG_LOG")
    export OWG_OWNER OWG_OWNER_LOG
  else
    unset OWG_OWNER OWG_OWNER_LOG
  fi
}

installed_version() {
  local file=${CHECKOUT:-$DIR}/pyproject.toml
  if [[ -f $file ]]; then sed -n 's/^version = "\(.*\)"/\1/p' "$file" | head -1; else printf '%s' "$OWG_VERSION"; fi
}

is_installed() {
  [[ -x $1/.venv/bin/python ]] && compgen -G "$1/settings/*.toml" >/dev/null || return 1
  [[ $(receipt_get state) != pending ]] || return 1
  receipt_present && return 0
  # Legacy deployments have no receipt. A virtualenv plus copied settings alone is incomplete.
  local unit command unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
  for unit in loadcoach weightroom turnstile toollocker spotter voice voice-gateway frontdesk tooldoor; do
    [[ -f $unit_dir/$unit.service ]] || continue
    command=$(sed -n 's/^ExecStart=//p' "$unit_dir/$unit.service" | head -1)
    command=${command//%h/$HOME}
    [[ $command == "$1"/.venv/bin/* ]] && return 0
  done
  return 1
}

# Restore a confirmed plan without saving passwords, API keys or the consumed pairing code.
resume_settings() {
  local pair variable key value
  for pair in TOPOLOGY:topology ADMIN:admin DATABASE:database LLAMA:llama STARTER:starter VOICE:voice \
    LINGER_WANTED:linger_wanted POWER_RULE:power_rule FRONT_URL:front_url COMPUTE_URL:compute_url \
    MACHINE_NAME:machine_name JOIN_URL:join_url; do
    variable=${pair%%:*} key=${pair#*:}
    value=$(receipt_get "$key")
    [[ -z $value || -n ${PLAN_GIVEN[$variable]:-} ]] || printf -v "$variable" '%s' "$value"
  done
  CREATED_DIR=${RECEIPT[created_dir]:-0}
  LINGER_ENABLED=${RECEIPT[linger_enabled]:-0}
  UV_DOWNLOADED=${RECEIPT[uv]:+1}
  POLKIT_RULE=$(receipt_get polkit_rule)
  if [[ -n $JOIN_URL ]]; then PAIRED=1; fi
}

# Something this run is allowed to start: the user's systemd, or the machine's for a system installation.
can_start() { if ((SYSTEM)); then ((SYSTEMD)); else ((SYSTEMD_USER)); fi; }

# ---- warm-up --------------------------------------------------------------------------------

warm_up() {
  ui_section "Warm-up" "Checking your machine"
  detect_os
  detect_package_manager
  detect_systemd
  detect_gpu
  detect_address
  detect_disk "$DIR"

  [[ $(uname -s) == Linux ]] || ui_die "openWeightGym runs on Linux." "On Windows, use WSL2 with systemd turned on; macOS is not supported yet."
  case "$OS_ARCH" in
    x86_64|aarch64|arm64) ;;
    *) ui_die "This processor ($OS_ARCH) is not supported." "openWeightGym needs a 64-bit x86 or ARM machine." ;;
  esac
  ui_ok "$OS_NAME" "$OS_ARCH$( ((OS_WSL)) && printf ' · WSL')"

  if ((EUID == 0 && !SYSTEM)); then
    ui_warn "Running as root" "the services will run as root; --system runs them as their own account instead"
  fi

  MISSING_TOOLS=()
  local tool
  for tool in git tar; do command -v "$tool" >/dev/null || MISSING_TOOLS+=("$tool"); done
  if ((${#MISSING_TOOLS[@]})); then ui_warn "Not installed: ${MISSING_TOOLS[*]}" "they will be installed with ${PKG_MANAGER:-your package manager}"
  else ui_ok "git, curl and tar"; fi

  if ((SYSTEM)); then
    ((SYSTEMD)) || ui_die "A system installation needs systemd running this machine." "Install for yourself instead, without --system."
    ui_ok "systemd" "services run as the $SVC account, from boot"
  elif ((SYSTEMD_USER)); then
    if ((LINGER)); then ui_ok "systemd user services" "kept running after you log out"
    else ui_ok "systemd user services"; fi
  else
    ui_warn "systemd user services are not running for this account" "services will be set up but must be started by hand"
    ((OS_WSL)) && ui_note "In WSL, add [boot] systemd=true to /etc/wsl.conf, run wsl --shutdown in Windows, and start again."
  fi

  case "$GPU_KIND" in
    nvidia)
      local count=''
      ((GPU_COUNT > 1)) && count=" ×$GPU_COUNT"
      ui_ok "$GPU_NAME$count" "$(((GPU_VRAM_MB + 512) / 1024)) GB$( ((GPU_COUNT > 1)) && printf ' each') · CUDA ${GPU_CUDA:-unknown}" ;;
    amd|intel) ui_ok "${GPU_NAME:-$GPU_KIND graphics}" "llama.cpp: $LLAMA_BACKEND" ;;
    *) ui_info "No graphics card found" "models will run on the processor" ;;
  esac

  if command -v uv >/dev/null && ((!SYSTEM)); then ui_ok "uv $(uv --version 2>/dev/null | awk '{print $2}')" "for Python 3.13"
  else ui_ok "Python 3.13" "fetched with uv into the installation; the system's Python is left alone"; fi

  if ((DISK_FREE_MB < 3000)); then
    ui_die "Only $((DISK_FREE_MB / 1024)) GB free where $DIR would go." "openWeightGym needs about 3 GB, and models need more. Choose another place with --dir."
  elif ((DISK_FREE_MB < 8000)); then
    ui_warn "$((DISK_FREE_MB / 1024)) GB free" "enough to start; models need more"
  else
    ui_ok "$((DISK_FREE_MB / 1024)) GB free" "for ${DIR/#$HOME/\~}"
  fi

  ((SYSTEM)) && return 0
  # Units of the same names, run from another folder, would be replaced: stop before that.
  local unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user unit command other=''
  for unit in loadcoach weightroom turnstile toollocker spotter voice voice-gateway frontdesk tooldoor; do
    [[ -f $unit_dir/$unit.service ]] || continue
    command=$(sed -n 's/^ExecStart=//p' "$unit_dir/$unit.service" | head -1)
    command=${command//%h/$HOME}
    if [[ $command == */.venv/bin/* && ${command%%/.venv/bin/*} != "$DIR" ]]; then other=${command%%/.venv/bin/*}; break; fi
  done
  if [[ -n $other ]]; then
    ui_die "This account already runs openWeightGym from $other." \
      "Update that installation with $other/install.sh, or install this one with --dir $other."
  fi
}

# ---- an existing installation ---------------------------------------------------------------

installed_menu() {
  ui_section "Welcome back" "openWeightGym is installed in ${DIR/#$HOME/\~}"
  local choice=0
  local -a actions=(update repair) labels=(
    "Update|fetch the latest openWeightGym and reinstall, keeping settings and data"
    "Repair|reinstall packages and services as they are")
  if [[ ,$(receipt_get services), == *,weightroom,* ]]; then
    actions+=(pair)
    labels+=("Add a GPU node|make a one-time code another machine joins with")
  fi
  actions+=(uninstall quit)
  labels+=("Uninstall|remove openWeightGym, choosing what to keep" "Quit|change nothing")
  ui_choose choice "What would you like to do?" 0 "${labels[@]}"
  case "${actions[choice]}" in
    update) update ;;
    repair) reinstall ;;
    pair) as_owner "$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml" pair ;;
    uninstall) run_uninstaller ;;
    *) ui_info "Nothing changed." ;;
  esac
}

update() {
  if [[ -d $DIR/.git ]]; then
    if [[ -n $(as_owner git -C "$DIR" status --porcelain --untracked-files=no) ]]; then
      ui_die "$DIR has changes of your own." "Commit or stash them, then update again."
    fi
    local before
    before=$(as_owner git -C "$DIR" rev-parse --short HEAD)
    ui_step "Fetching the latest openWeightGym" -- as_owner git -C "$DIR" pull --ff-only ||
      ui_die "Could not fetch the latest openWeightGym." "The log says why: $OWG_LOG"
    if [[ $(as_owner git -C "$DIR" rev-parse --short HEAD) == "$before" ]]; then ui_ok "Already up to date" "$before"
    else ui_ok "Updated" "$before → $(as_owner git -C "$DIR" rev-parse --short HEAD)"; fi
  else
    ui_warn "$DIR is not a git checkout" "reinstalling the code that is there"
  fi
  reinstall
}

# The uninstaller beside this installer, or the one on GitHub under `curl | bash`.
run_uninstaller() {
  local script=${CHECKOUT:-$DIR}/uninstall.sh
  [[ -x $DIR/uninstall.sh ]] && script=$DIR/uninstall.sh
  if [[ -x $script ]]; then exec "$script" --dir "$DIR" "${PASSTHROUGH[@]}"; fi
  exec bash -c 'curl -fsSL "$1" | bash -s -- "${@:2}"' _ "$OWG_RAW/$OWG_REF/uninstall.sh" --dir "$DIR" "${PASSTHROUGH[@]}"
}

reinstall() {
  find_uv
  ui_section "Setting up" "Packages, settings and services"
  as_owner "$DIR/scripts/install-suite.sh" --root "$DIR" --embedded --non-interactive --yes --database keep --uv "$UV" ||
    ui_die "Reinstalling did not finish." "The log says why: ${OWG_OWNER_LOG:-$OWG_LOG}"
  services_from_receipt
  if ! receipt_present && [[ ,$SERVICES, == *,weightroom,* ]] && ! console_account_ready; then
    if [[ ,$SERVICES, == *,loadcoach,* ]]; then TOPOLOGY=all; else TOPOLOGY=front; fi
    STARTER=0 DATABASE=keep
    account
    finishing
    spotting
    write_receipt complete
    finale
    return 0
  fi
  link_owg
  receipt_publish "$DIR"
  ui_section "Spotting" "Checking it all works"
  check_services
  ui_finale "Back in shape"
}

# A legacy or interrupted front may have only the weightroom service identity, with no sign-in.
console_account_ready() {
  local listing name
  listing=$(as_owner "$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml" user list) || return 1
  while IFS= read -r name; do
    if as_owner "$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml" user show "$name" | grep -qE '^So it may:.*superuser'; then return 0; fi
  done < <(awk 'NR > 1 && $3 == "yes" && $4 != "-" {print $1}' <<<"$listing")
  return 1
}

# ---- the game plan --------------------------------------------------------------------------

game_plan() {
  ui_section "Game plan" "How this machine fits in"
  if [[ -z $TOPOLOGY ]]; then
    local choice=0
    ui_choose choice "How should openWeightGym run here?" 0 \
      "Everything on this machine|the console, the API and the models together: the quickest start" \
      "Console only|the web console and API; GPU machines join it later" \
      "GPU node|add this machine's graphics cards to a console you already run"
    local -a topologies=(all front compute)
    TOPOLOGY=${topologies[choice]}
  fi
  [[ $TOPOLOGY != front ]] || STARTER=0
  if [[ $TOPOLOGY == compute ]]; then ask_console; fi
  if [[ $TOPOLOGY == front && -z $COMPUTE_URL ]] && ((!ASSUME_YES)); then
    local node=''
    ui_input node "A GPU node's address, if you have one already (Enter to add one later)" "" valid_host_or_empty
    [[ -z $node ]] || COMPUTE_URL=http://$node:8442
  fi

  while true; do
    show_plan
    ((ASSUME_YES)) && break
    local choice=0
    ui_choose choice "Ready?" 0 "Install|with these choices" "Customize|change any of them" "Quit|install nothing"
    case "$choice" in
      0) break ;;
      1) customize ;;
      *) ui_info "Nothing was installed."; exit 0 ;;
    esac
  done
  # The code is used once the plan is settled, and before anything is downloaded.
  if [[ -n $JOIN_URL ]]; then redeem; fi
}

valid_host() {
  [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] && return 0
  ui_warn "That is not an address: use an IP address such as 192.168.1.10, or a host name"
  return 1
}
valid_host_or_empty() { [[ -z $1 ]] || valid_host "$1"; }
valid_console() { local host=${1#*://}; host=${host%%[:/]*}; valid_host "$host"; }
valid_code() {
  local code=${1//[- ]/}
  [[ ${code^^} =~ ^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$ ]] && return 0
  ((UI_ASK)) && ui_warn "A pairing code is eight letters and digits, such as ABCD-EF23"
  return 1
}

# Use the pairing code: the console answers with this node's name, the key the two sign with and
# turnstile's address, and adds the node to its settings.
redeem() {
  local body answer status json name
  name=$(uname -n | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')
  body=$(printf '{"code": "%s", "name": "%s", "address": "%s", "voice": %s}' "${PAIR_CODE^^}" "$name" "$HOST_ADDRESS" "$( ((VOICE)) && printf true || printf false)")
  answer=$(curl -sS --max-time 30 -X POST -H 'content-type: application/json' -d "$body" -w '\n%{http_code}' "$JOIN_URL/pair" 2>>"$OWG_LOG") ||
    ui_die "Could not reach the console at $JOIN_URL." "Check the address, and that this machine can reach it on the network."
  status=${answer##*$'\n'} json=${answer%$'\n'*}
  if [[ $status != 200 ]]; then
    ui_die "The console did not accept the code: $(json_field message "$json")" "Make a new code on the console and run this again."
  fi
  MACHINE_KEY=$(json_field machine_key "$json") MACHINE_NAME=$(json_field name "$json") FRONT_URL=$(json_field front "$json")
  VOICE_KEY=$(json_field voice_key "$json")
  [[ -n $MACHINE_KEY && -n $FRONT_URL ]] || ui_die "The console's answer was not understood." "Update the console and make a new pairing code."
  if ((VOICE)) && [[ -z $VOICE_KEY ]]; then ui_die "The console did not provide a voice-service key." "Update the console and make a new pairing code."; fi
  PAIRED=1
  ui_ok "Joined the console at ${JOIN_URL#*://}" "as $MACHINE_NAME"
}

# One string field of a flat JSON answer.
json_field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" <<<"$2" | head -1; }

ask_console() {
  if ((VOICE)) && [[ -n $VOICE_KEY_FILE ]]; then
    [[ -r $VOICE_KEY_FILE ]] || ui_die "Cannot read the voice-service key in $VOICE_KEY_FILE."
  fi
  if [[ -z $JOIN_URL && -z $FRONT_URL ]] && ((!ASSUME_YES)); then
    local way=0 host=''
    ui_choose way "How should this node join the console?" 0 \
      "With a pairing code|made on the console by owg pair, or Settings → Compute nodes" \
      "With the console's key|copied from its keys/machine.key by hand"
    if ((way == 0)); then
      ui_input host "The console's address (as in its web address, such as 192.168.1.10)" "" valid_console
      JOIN_URL=$(console_url "$host")
    fi
  fi
  if [[ -n $JOIN_URL ]]; then
    JOIN_URL=$(console_url "$JOIN_URL")
    [[ -n $PAIR_CODE ]] || ((ASSUME_YES)) || ui_input PAIR_CODE "The pairing code" "" valid_code
    valid_code "$PAIR_CODE" || ui_die "A pairing code looks like ABCD-EF23." "Make one on the console: owg pair, or Settings → Compute nodes."
    return 0
  fi
  if [[ -z $FRONT_URL ]]; then
    local host=''
    ((ASSUME_YES)) && ui_die "A GPU node needs --front-url, the console's address."
    ui_input host "The console machine's address (its IP or host name)" "" valid_host
    FRONT_URL=http://$host:8442
  fi
  if [[ -n $MACHINE_KEY_FILE ]]; then
    [[ -r $MACHINE_KEY_FILE ]] || ui_die "Cannot read $MACHINE_KEY_FILE."
    MACHINE_KEY=$(<"$MACHINE_KEY_FILE")
  elif [[ -r $DIR/keys/machine.key ]]; then
    MACHINE_KEY=$(<"$DIR/keys/machine.key")
  else
    ((ASSUME_YES)) && ui_die "A GPU node needs --machine-key-file, a copy of the console's keys/machine.key."
    ui_note "The console and this node sign what they send each other with one shared key."
    ui_note "On the console, show it with:  cat ~/openWeightGym/keys/machine.key"
    ui_secret MACHINE_KEY "Paste the console's machine key" 20 once
  fi
  if ((VOICE)) && [[ -z $VOICE_KEY_FILE && ! -s $DIR/keys/voice-service.key ]]; then
    ui_die "Voice needs its own service key from the console." "Join with a pairing code, or copy the console's keys/voice-service.key and pass --voice-key-file FILE."
  fi
}

backend_label() {
  case "$1" in
    cuda-*) printf 'NVIDIA CUDA %s' "${1#cuda-}" ;;
    rocm) printf 'AMD ROCm' ;;
    vulkan) printf 'Vulkan' ;;
    cpu) printf 'processor only' ;;
    source) printf 'built here from source' ;;
    skip) printf 'not now' ;;
    *) printf 'your own: %s' "$1" ;;
  esac
}

services_for() {
  case "$TOPOLOGY" in
    all) SERVICES=loadcoach,toollocker,spotter,weightroom,turnstile; ((VOICE)) && SERVICES+=,voice,voice-gateway ;;
    front) SERVICES=weightroom,turnstile; ((VOICE)) && SERVICES+=,voice-gateway ;;
    compute) SERVICES=loadcoach,toollocker,spotter; ((VOICE)) && SERVICES+=,voice ;;
  esac
  return 0
}

# What each program is, in a word or two, for people who have not met them yet.
plain_programs() {
  local service out=''
  for service in ${SERVICES//,/ }; do
    case "$service" in
      weightroom) out+=", web console" ;;
      turnstile) out+=", API gateway" ;;
      loadcoach) out+=", model runner" ;;
      toollocker) out+=", tools" ;;
      spotter) out+=", monitor" ;;
      voice) out+=", voice" ;;
      voice-gateway) out+=", voice gateway" ;;
    esac
  done
  printf '%s' "${out#, }"
}

show_plan() {
  services_for
  local where=$TOPOLOGY engine=auto lines=()
  case "$TOPOLOGY" in
    all) where="Everything on this machine" ;;
    front) where="Console only" ;;
    compute) where="GPU node for ${JOIN_URL:-$FRONT_URL}"; where=${where/http:\/\//} ;;
  esac
  [[ $LLAMA == auto ]] && engine=$LLAMA_BACKEND || engine=$LLAMA
  lines+=("${C_MUTED}Setup        ${C_RESET}$where")
  lines+=("${C_MUTED}Install to   ${C_RESET}${DIR/#$HOME/\~}")
  if ((SYSTEM)); then
    lines+=("${C_MUTED}Runs as      ${C_RESET}the $SVC account, from every boot")
  elif ((SYSTEMD_USER)); then
    lines+=("${C_MUTED}Runs as      ${C_RESET}you, $( ((LINGER_WANTED)) && printf 'from every boot, signed in or not' || printf 'while you are signed in')")
  fi
  lines+=("${C_MUTED}Programs     ${C_RESET}$(plain_programs)")
  lines+=("${C_MUTED}Data         ${C_RESET}$([[ $DATABASE == keep ]] && printf 'keep existing stores' || { [[ $DATABASE == sqlite ]] && printf 'SQLite, on this machine' || printf 'PostgreSQL, on this machine'; })")
  if [[ $TOPOLOGY != front ]]; then
    lines+=("${C_MUTED}Engine       ${C_RESET}llama.cpp · $(backend_label "$engine")")
    if ((STARTER)); then lines+=("${C_MUTED}First model  ${C_RESET}$STARTER_TITLE, about 1.8 GB")
    else lines+=("${C_MUTED}First model  ${C_RESET}none for now"); fi
    ((POWER_RULE)) && lines+=("${C_MUTED}Power        ${C_RESET}the console may sleep, restart or switch off this machine")
  fi
  ui_box "Your setup" "${lines[@]}"
}

customize() {
  local choice=0 answer=''
  if ((SYSTEMD)); then
    ui_choose choice "Who should openWeightGym run as?" "$SYSTEM" \
      "Just me|in ~/openWeightGym, as your own services" \
      "The whole machine|in /opt/openweightgym as its own $SVC account, from boot; needs sudo"
    SYSTEM=$choice
    if ((!DIR_GIVEN)); then
      if ((SYSTEM)); then DIR=$SYS_OPT; else DIR=${CHECKOUT:-$HOME/openWeightGym}; fi
    fi
    system_mode_settings
  fi
  if [[ -z $CHECKOUT ]] && ((!SYSTEM)); then
    ui_input answer "Install to" "$DIR"
    DIR=$(realpath -m -- "${answer/#\~/$HOME}")
  fi
  ui_confirm "Install speech recognition and voices too? (large; for the Voice pages)" n && VOICE=1 || VOICE=0
  if ((SYSTEM)); then
    DATABASE=sqlite
  else
    ui_choose choice "Where should openWeightGym keep its data?" 0 \
      "SQLite|files in data/, nothing else to run" "PostgreSQL|a database server on this machine, set up for you"
    local -a databases=(sqlite postgresql)
    DATABASE=${databases[choice]}
  fi
  if [[ $TOPOLOGY != front ]]; then
    ui_choose choice "Which llama.cpp should run the models?" 0 \
      "Prebuilt for this machine|$(backend_label "$LLAMA_BACKEND")" \
      "Prebuilt, another kind|choose CUDA, ROCm, Vulkan or processor only" \
      "One I already have|give the path of your llama-server" \
      "Build it here|from source with cmake; slow" \
      "Not now|install the engine later"
    case "$choice" in
      0) LLAMA=auto ;;
      1)
        ui_choose choice "Which build?" 0 "cuda-13.4|NVIDIA, driver 580 or newer" "cuda-12.8|NVIDIA, older drivers" \
          "vulkan|NVIDIA, AMD or Intel" "rocm|AMD with ROCm installed" "cpu|no graphics card"
        local -a kinds=(cuda-13.4 cuda-12.8 vulkan rocm cpu)
        LLAMA=${kinds[choice]} ;;
      2)
        ui_input answer "Path of your llama-server" "$(command -v llama-server || true)" valid_program
        LLAMA=$answer ;;
      3) LLAMA=source ;;
      4) LLAMA=skip ;;
    esac
    if [[ $LLAMA != skip ]]; then
      ui_confirm "Download $STARTER_TITLE (about 1.8 GB) so you can chat straight away?" y && STARTER=1 || STARTER=0
    else
      STARTER=0
    fi
    ui_confirm "Let the console put this machine to sleep, restart it or switch it off?" n && POWER_RULE=1 || POWER_RULE=0
  fi
  if ((SYSTEMD_USER && !SYSTEM)); then
    ui_confirm "Keep the services running when you are not signed in?" y && LINGER_WANTED=1 || LINGER_WANTED=0
  fi
}

valid_program() {
  [[ -x $1 && ! -d $1 ]] && return 0
  ui_warn "$1 is not a program here"
  return 1
}

# ---- the first account ----------------------------------------------------------------------

account() {
  [[ $TOPOLOGY == compute ]] && return 0
  ui_section "Account" "Your sign-in for the console"
  local default=${USER:-admin}
  default=$(printf '%s' "${default,,}" | tr -cd 'a-z0-9._-')
  [[ -n $default ]] || default='admin'
  if [[ -z $ADMIN ]]; then ui_input ADMIN "Your user name" "$default" valid_user_name; fi
  valid_user_name "$ADMIN" || ui_die "$ADMIN cannot be a user name."
  PASSWORD=${OWG_ADMIN_PASSWORD:-}
  [[ -z $PASSWORD || ${#PASSWORD} -ge 8 ]] || ui_die "The administrator password must have at least 8 characters."
  if [[ -z $PASSWORD ]]; then
    if ! ui_secret PASSWORD "A password for $ADMIN (8 characters or more)" 8; then
      PASSWORD=''
      ui_info "No terminal to type a password: a temporary one is shown at the end"
    fi
  fi
  ui_ok "Account $ADMIN" "administrator"
}

valid_user_name() {
  [[ $1 =~ ^[a-z0-9._-]{1,64}$ ]] && return 0
  ui_warn "Lowercase letters, digits, dot, dash or underscore, please"
  return 1
}

# ---- loading plates: the downloads ----------------------------------------------------------

loading_plates() {
  ui_section "Loading plates" "Fetching what is needed"
  install_tools
  ((SYSTEM)) && prepare_system
  fetch_code
  save_machine_key
  services_for
  write_receipt pending
  find_uv
  ui_step "Python 3.13" -- as_owner "$UV" python install --no-bin 3.13 ||
    ui_die "uv could not fetch Python 3.13." "Check the network; the log has uv's own words."
  [[ $TOPOLOGY == front ]] || engine
  if ((STARTER)); then starter_model; fi
}

install_tools() {
  ((${#MISSING_TOOLS[@]})) || return 0
  ((${#PKG_INSTALL[@]})) || ui_die "Please install ${MISSING_TOOLS[*]} and run the installer again."
  local packages=() tool
  for tool in "${MISSING_TOOLS[@]}"; do packages+=("$(package_for "$tool")"); done
  ui_sudo "installing ${packages[*]}"
  [[ $PKG_MANAGER == apt ]] && ui_step "Refreshing the package list" -- sudo -n apt-get update
  ui_step "Installing ${packages[*]}" -- sudo -n "${PKG_INSTALL[@]}" "${packages[@]}" ||
    ui_die "Could not install ${packages[*]}." "Install them yourself and run the installer again."
}

# The account and folders of a system installation. The service account's own systemd, kept
# running by linger, starts the programs at boot; its files follow the usual places:
#   /opt/openweightgym       the code, its Python and the engine builds
#   /etc/openweightgym/keys  the secrets (opt/keys links here), and settings, a link to opt/settings
#   /var/lib/openweightgym   the account's home: data/ and models/ (opt/data and opt/models link here)
prepare_system() {
  ui_sudo "making the $SVC account and its folders"
  if ! id "$SVC" >/dev/null 2>&1; then
    ui_step "Making the $SVC account" -- sudo -n useradd --system --user-group --home-dir "$SYS_VAR" \
      --shell "$(command -v nologin || printf /bin/false)" --comment "openWeightGym services" "$SVC" ||
      ui_die "Could not make the $SVC account."
  else
    ui_ok "The $SVC account is already here"
  fi
  local group
  for group in video render; do
    getent group "$group" >/dev/null && sudo -n usermod -aG "$group" "$SVC" 2>>"$OWG_LOG" || true
  done
  sudo -n install -d -o "$SVC" -g "$SVC" -m 755 "$SYS_VAR" "$SYS_VAR/models" "$SYS_OPT"
  sudo -n install -d -o "$SVC" -g "$SVC" -m 750 "$SYS_VAR/data" "$SYS_VAR/logs"
  sudo -n install -d -o root -g root -m 755 "$SYS_ETC"
  sudo -n install -d -o "$SVC" -g "$SVC" -m 700 "$SYS_ETC/keys"
  sudo -n install -o "$SVC" -g "$SVC" -m 600 /dev/null "$OWG_OWNER_LOG"
  ui_ok "Folders" "${SYS_OPT}, ${SYS_ETC}, ${SYS_VAR}"
  if [[ $(loginctl show-user "$SVC" -p Linger --value 2>/dev/null) != yes ]]; then
    sudo -n loginctl enable-linger "$SVC" || ui_die "Could not let the $SVC account start at boot (loginctl enable-linger)."
    LINGER_ENABLED=1
  fi
  # Its systemd starts a moment after linger is turned on.
  local uid i
  uid=$(id -u "$SVC")
  for ((i = 0; i < 20; i++)); do [[ -S /run/user/$uid/bus ]] && break; sleep 0.5; done
  [[ -S /run/user/$uid/bus ]] && ui_ok "The $SVC account starts at boot" || ui_warn "The $SVC account's systemd has not started yet"
}

# A system installation's data, keys and models live outside the code folder, linked from it.
link_system_folders() {
  local name target
  for name in data:"$SYS_VAR/data" keys:"$SYS_ETC/keys" models:"$SYS_VAR/models"; do
    target=${name#*:} name=${name%%:*}
    [[ -L $DIR/$name ]] || as_owner ln -sfn "$target" "$DIR/$name"
  done
  [[ -L $SYS_ETC/settings ]] || sudo -n ln -sfn "$DIR/settings" "$SYS_ETC/settings"
}

fetch_code() {
  if [[ -n $CHECKOUT && $DIR == "$CHECKOUT" ]]; then
    ui_ok "openWeightGym $(installed_version)" "this checkout, ${DIR/#$HOME/\~}"
    CREATED_DIR=${CREATED_DIR:-0}
    return 0
  fi
  if [[ -e $DIR ]] && [[ -n $(ls -A "$DIR" 2>/dev/null) ]] && [[ ! -f $DIR/pyproject.toml ]] && receipt_present; then
    # What an earlier uninstall kept: settings, keys, data or models. Put the code back around them.
    ui_info "Found what an earlier installation kept in ${DIR/#$HOME/\~}" "it is used as it is"
    local clone=$DIR/.owg-clone
    as_owner rm -rf "$clone"
    ui_step "Fetching openWeightGym ($OWG_REF)" -- as_owner git clone --quiet --no-checkout --branch "$OWG_REF" "$OWG_REPO" "$clone" ||
      ui_die "Could not fetch openWeightGym from $OWG_REPO."
    as_owner mv "$clone/.git" "$DIR/.git"
    as_owner rmdir "$clone"
    as_owner git -C "$DIR" reset --quiet --hard
    CREATED_DIR=1
  elif [[ -e $DIR ]] && [[ -n $(ls -A "$DIR" 2>/dev/null) ]]; then
    [[ -f $DIR/pyproject.toml && -f $DIR/scripts/install-suite.sh ]] ||
      ui_die "$DIR exists and is not openWeightGym." "Choose another place with --dir, or empty that folder."
    ui_ok "openWeightGym is already in ${DIR/#$HOME/\~}"
    CREATED_DIR=${CREATED_DIR:-0}
  else
    ((SYSTEM)) || mkdir -p "$(dirname "$DIR")"
    ui_step "Fetching openWeightGym ($OWG_REF)" -- as_owner git clone --quiet --branch "$OWG_REF" "$OWG_REPO" "$DIR" ||
      ui_die "Could not fetch openWeightGym from $OWG_REPO."
    CREATED_DIR=1
  fi
  CHECKOUT=$DIR
  ((SYSTEM)) && link_system_folders
  return 0
}

# uv makes the Python environment. One already on this account's PATH is used; otherwise (and
# always for a system installation) a pinned copy goes in the installation's .tools folder.
# The Python it fetches lives in .python, beside it.
find_uv() {
  export UV_PYTHON_INSTALL_DIR=$DIR/.python
  if [[ -x $DIR/.tools/uv ]]; then UV=$DIR/.tools/uv; return 0; fi
  if command -v uv >/dev/null && ((!SYSTEM)); then UV=$(command -v uv); return 0; fi
  local arch=$OS_ARCH work
  [[ $arch == arm64 ]] && arch=aarch64
  work=$(mktemp -d)
  chmod 755 "$work"
  ui_download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-unknown-linux-gnu.tar.gz" \
    "$work/uv.tar.gz" "uv $UV_VERSION" || ui_die "Could not download uv."
  as_owner mkdir -p "$DIR/.tools"
  as_owner tar -xzf "$work/uv.tar.gz" -C "$DIR/.tools" --strip-components=1
  rm -rf "$work"
  UV=$DIR/.tools/uv
  UV_DOWNLOADED=1
}

engine() {
  LLAMA_SERVER=''
  case "$LLAMA" in
    skip) ui_info "llama.cpp: skipped" "install it later with scripts/install-llamacpp.sh"; return 0 ;;
    /*) [[ -x $LLAMA ]] || ui_die "$LLAMA is not a program."; LLAMA_SERVER=$LLAMA; ui_ok "llama.cpp: your own $LLAMA"; return 0 ;;
  esac
  local options=(--prefix "$DIR/runtimes/llamacpp") runner=()
  ((ASSUME_YES)) && options+=(--yes)
  case "$LLAMA" in
    auto) ;;
    source) options+=(--from-source) ;;
    *) options+=(--backend "$LLAMA") ;;
  esac
  # A system installation's engine is fetched as root, which may add libraries, then handed to its account.
  ((SYSTEM)) && runner=(sudo -n env "OWG_LOG=$OWG_LOG" "PATH=$PATH" "TERM=${TERM:-dumb}")
  if ! "${runner[@]}" "$DIR/scripts/install-llamacpp.sh" "${options[@]}"; then
    ui_warn "llama.cpp did not install" "the rest goes on; run scripts/install-llamacpp.sh to try again"
    STARTER_REGISTER=0
    return 0
  fi
  ((SYSTEM)) && sudo -n chown -R "$SVC:$SVC" "$DIR/runtimes"
  LLAMA_SERVER=$DIR/runtimes/llamacpp/current/llama-server
}

starter_model() {
  local folder=$DIR/models/llm
  local model=$folder/$STARTER_FILE
  if [[ -s $model ]]; then ui_ok "$STARTER_TITLE is already here"; return 0; fi
  # Downloaded as you, into a folder that is yours for the length of the download.
  if ((SYSTEM)); then sudo -n install -d -o "$(id -un)" -m 755 "$SYS_VAR/models/llm"; fi
  if ! ui_download "$STARTER_URL" "$model" "$STARTER_TITLE"; then
    ui_warn "Skipping the starter model" "add models later from the console"
    STARTER=0
  fi
  if ((SYSTEM)); then sudo -n chown -R "$SVC:$SVC" "$SYS_VAR/models/llm"; fi
  return 0
}

# ---- setting up -----------------------------------------------------------------------------

save_machine_key() {
  if [[ $TOPOLOGY == compute && -n ${MACHINE_KEY:-} ]] && { ((PAIRED)) || ! as_owner test -f "$DIR/keys/machine.key"; }; then
    as_owner mkdir -p "$DIR/keys"
    as_owner chmod 700 "$DIR/keys"
    printf '%s\n' "$MACHINE_KEY" | as_owner bash -c 'umask 077 && cat >"$1"' _ "$DIR/keys/machine.key"
    ui_ok "Saved the console's machine key"
  fi
  if [[ $TOPOLOGY == compute ]] && ((VOICE)); then
    [[ -z $VOICE_KEY_FILE ]] || VOICE_KEY=$(<"$VOICE_KEY_FILE")
    if [[ -n $VOICE_KEY ]]; then
      printf '%s\n' "$VOICE_KEY" | as_owner bash -c 'umask 077 && cat >"$1" && chmod 600 "$1"' _ "$DIR/keys/voice-service.key"
    fi
  fi
}

setting_up() {
  ui_section "Setting up" "Python, settings and services"
  services_for
  save_machine_key
  local options=(--root "$DIR" --embedded --non-interactive --yes --services "$SERVICES" --topology "$TOPOLOGY"
                 --address "$HOST_ADDRESS" --start-new --uv "$UV" --database "$DATABASE")
  [[ -n $FRONT_URL ]] && options+=(--front-url "$FRONT_URL")
  [[ -n $COMPUTE_URL ]] && options+=(--compute-url "$COMPUTE_URL")
  [[ -n ${LLAMA_SERVER:-} ]] && options+=(--llama-server "$LLAMA_SERVER")
  [[ -n $MACHINE_NAME ]] && options+=(--machine-name "$MACHINE_NAME")
  ((VOICE)) && options+=(--voice-model "$STARTER_NAME")
  [[ $DATABASE == postgresql ]] && options+=(--install-postgresql --provision-postgresql)
  if [[ $DATABASE == postgresql ]]; then ui_sudo "installing and setting up PostgreSQL"; fi
  as_owner "$DIR/scripts/install-suite.sh" "${options[@]}" ||
    ui_die "Setting up did not finish." "The log says why: ${OWG_OWNER_LOG:-$OWG_LOG}"
}

finishing() {
  local weightroom=(as_owner "$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml")
  local loadcoach=(as_owner "$DIR/.venv/bin/loadcoach" --config "$DIR/settings/loadcoach.toml")
  API_KEY='' TEMPORARY=''
  if [[ $TOPOLOGY != compute ]]; then
    local password_state
    password_state=$("${weightroom[@]}" user list | awk -v name="$ADMIN" 'NR > 1 && $1 == name {print $4}')
    if [[ -n $password_state ]]; then
      ui_ok "Account $ADMIN is already there"
    else
      local said
      said=$("${weightroom[@]}" user add "$ADMIN" superuser 2>>"$OWG_LOG") || ui_die "Could not add the account $ADMIN."
      API_KEY=$(printf '%s\n' "$said" | grep -oE 'lc_[A-Za-z0-9_-]+' | tail -1)
    fi
    # Account creation may have completed just before an interrupted password write.
    if [[ -z $password_state || $password_state == - ]]; then
      if [[ -n $PASSWORD ]]; then
        printf '%s\n' "$PASSWORD" | "${weightroom[@]}" user password "$ADMIN" --stdin >>"$OWG_LOG" 2>&1 ||
          ui_die "Could not set $ADMIN's password."
      else
        TEMPORARY=$("${weightroom[@]}" user temporary "$ADMIN" 2>>"$OWG_LOG" | tail -1)
      fi
      ui_ok "Set up the account $ADMIN"
    fi
  fi
  if ((STARTER)) && [[ $TOPOLOGY != front && ${STARTER_REGISTER:-1} == 1 && -n ${LLAMA_SERVER:-} ]]; then
    if "${loadcoach[@]}" models list 2>/dev/null | grep -qw "$STARTER_NAME"; then
      ui_ok "$STARTER_NAME is already set up"
    else
      local flags=("ctx-size=$STARTER_CONTEXT")
      if [[ $GPU_KIND == none || $LLAMA == cpu || $LLAMA_BACKEND == cpu && $LLAMA == auto ]]; then flags+=(gpus=0); else flags+=(gpus=1 gpu-layers=all); fi
      if "${loadcoach[@]}" models add "$STARTER_NAME" "$DIR/models/llm/$STARTER_FILE" "${flags[@]}" >>"$OWG_LOG" 2>&1; then
        ui_ok "Added $STARTER_NAME" "ask for it by that name"
      else
        ui_warn "Could not add $STARTER_NAME" "add it from Models in the console"
      fi
    fi
  fi
  if ((!SYSTEM && SYSTEMD_USER && LINGER_WANTED && !LINGER)); then
    if loginctl enable-linger "${USER:-$(id -un)}" 2>>"$OWG_LOG"; then
      ui_ok "Services keep running when you log out"
      LINGER_ENABLED=1
    else
      ui_warn "Could not keep the services running after logout" "run: sudo loginctl enable-linger ${USER:-\$USER}"
    fi
  fi
  ((POWER_RULE)) && power_rule
  link_owg
  write_receipt pending
}

# Let spotter put this machine to sleep, restart it or switch it off, through polkit.
power_rule() {
  POLKIT_RULE=/etc/polkit-1/rules.d/50-openweightgym-spotter.rules
  local account=${OWG_OWNER:-${USER:-$(id -un)}}
  [[ -d /etc/polkit-1/rules.d ]] || { ui_warn "polkit is not installed here" "the console cannot control power"; POLKIT_RULE=''; return 0; }
  ui_sudo "letting the console control this machine's power"
  sed -e "s/^var ACCOUNT = \".*\";/var ACCOUNT = \"$account\";/" -e 's/^var SERVICES = \[.*\];/var SERVICES = [];/' \
    "$DIR/services/spotter.rules" | sudo -n tee "$POLKIT_RULE" >/dev/null
  sudo -n chmod 644 "$POLKIT_RULE"
  ui_ok "The console may sleep, restart or switch off this machine" "$POLKIT_RULE"
}

# The owg command on the PATH, and a note of where the installation is: ~/.local/bin for you,
# /usr/local/bin for a system installation.
link_owg() {
  if ((SYSTEM)); then
    OWG_LINK=$SYS_BIN/owg POINTER=$SYS_ETC/install
    sudo -n ln -sfn "$DIR/scripts/owg" "$OWG_LINK"
    printf '%s\n' "$DIR" | sudo -n tee "$POINTER" >/dev/null
    ui_ok "The owg command" "owg status, owg logs, owg update, owg uninstall"
    return 0
  fi
  OWG_LINK=$HOME/.local/bin/owg POINTER=${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install
  mkdir -p "$(dirname "$OWG_LINK")" "$(dirname "$POINTER")"
  ln -sfn "$DIR/scripts/owg" "$OWG_LINK"
  printf '%s\n' "$DIR" >"$POINTER"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ui_ok "The owg command" "owg status, owg logs, owg update, owg uninstall" ;;
    *) ui_ok "The owg command" "in ~/.local/bin, which is not on your PATH yet: open a new terminal, or run ~/.local/bin/owg" ;;
  esac
}

# What this installer made, for the uninstaller: it removes only what is listed here.
write_receipt() {
  local unit_dir state=${1:-complete} owg_link pointer
  if ((SYSTEM)); then owg_link=$SYS_BIN/owg; pointer=$SYS_ETC/install
  else owg_link=$HOME/.local/bin/owg; pointer=${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install; fi
  # shellcheck disable=SC2016  # expanded by the owner's shell, in the owner's home
  unit_dir=$(as_owner bash -c 'printf %s "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"')
  {
    echo "# What install.sh made here, read by the uninstaller. Only what is listed is removed."
    echo "version=2"
    echo "state=$state"
    echo "installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "mode=$( ((SYSTEM)) && printf system || printf user)"
    echo "dir=$DIR"
    echo "created_dir=${CREATED_DIR:-0}"
    echo "topology=$TOPOLOGY"
    echo "services=$SERVICES"
    echo "unit_dir=$unit_dir"
    echo "python=$DIR/.python"
    echo "venv=$DIR/.venv"
    [[ ${UV_DOWNLOADED:-0} == 1 ]] && echo "uv=$DIR/.tools"
    [[ -d $DIR/runtimes/llamacpp ]] && echo "runtime=$DIR/runtimes/llamacpp"
    [[ -d $DIR/models ]] && echo "models=$DIR/models"
    echo "linger_enabled=${LINGER_ENABLED:-0}"
    echo "owg_link=${OWG_LINK:-$owg_link}"
    echo "pointer=${POINTER:-$pointer}"
    echo "database=$DATABASE"
    echo "llama=$LLAMA"
    echo "starter=$STARTER"
    echo "voice=$VOICE"
    echo "linger_wanted=$LINGER_WANTED"
    echo "power_rule=$POWER_RULE"
    echo "front_url=$FRONT_URL"
    echo "compute_url=$COMPUTE_URL"
    echo "machine_name=$MACHINE_NAME"
    echo "join_url=$JOIN_URL"
    [[ -n ${ADMIN:-} ]] && echo "admin=$ADMIN"
    [[ -n ${POLKIT_RULE:-} ]] && echo "polkit_rule=$POLKIT_RULE"
    if ((SYSTEM)); then
      echo "service_user=$SVC"
      echo "etc_dir=$SYS_ETC"
      echo "var_dir=$SYS_VAR"
    fi
  } | as_owner bash -c 'umask 022; cat >"$1.new" && chmod 644 "$1.new" && mv "$1.new" "$1"' _ "$DIR/.install-receipt"
}

# ---- spotting: checking it works ------------------------------------------------------------

wait_http() {  # wait_http URL SECONDS [curl options...]: until something answers, not 5xx or nothing
  local url=$1 seconds=$2 code
  shift 2
  for ((i = 0; i < seconds; i++)); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$@" "$url" 2>/dev/null) || code=000
    [[ $code =~ ^[234] ]] && return 0
    sleep 1
  done
  echo "$url did not answer within ${seconds}s (last: $code)"
  return 1
}

check_services() {
  local service failed=0
  if ! can_start; then
    ui_info "Start the services by hand: see docs/installation.md"
    return 0
  fi
  services_from_receipt
  for service in ${SERVICES//,/ }; do
    if owg_systemctl is-active --quiet "$service.service"; then ui_ok "$service is running"
    else ui_fail "$service is not running" "owg logs $service"; failed=1; fi
  done
  return "$failed"
}

services_from_receipt() {
  [[ -n ${SERVICES:-} ]] && return 0
  SERVICES=$(receipt_get services)
  if [[ -z $SERVICES ]]; then
    local unit found=()
    for unit in loadcoach toollocker spotter voice weightroom turnstile voice-gateway; do
      [[ -f $(receipt_unit_dir)/$unit.service ]] && found+=("$unit")
    done
    SERVICES=$(IFS=,; printf '%s' "${found[*]}")
  fi
  return 0
}

spotting() {
  ui_section "Spotting" "Checking it all works"
  can_start || { ui_info "systemd is not running here, so nothing was started"; return 0; }
  check_services
  local key
  # Wait only for programs that started: one that did not is already reported above.
  case "$TOPOLOGY" in
    all|front)
      if running weightroom; then ui_step "The console answers" -- wait_http http://127.0.0.1:8440/ 60 || true; fi
      if [[ $TOPOLOGY == all ]] && running turnstile; then
        key=$(as_owner cat "$DIR/keys/weightroom.key")
        ui_step "The API answers through the gateway" -- wait_http http://127.0.0.1:8442/v1/models 60 -H "Authorization: Bearer $key" || true
      fi ;;
    compute)
      if running loadcoach; then ui_step "LoadCoach answers on $HOST_ADDRESS" -- wait_http "http://$HOST_ADDRESS:8442/v1/models" 60 || true; fi ;;
  esac
  if [[ $TOPOLOGY == all ]] && ((STARTER)) && [[ -n $API_KEY ]] && running loadcoach; then first_rep; fi
}

running() { owg_systemctl is-active --quiet "$1.service" 2>/dev/null; }

# The first request: load the starter model and ask it something.
first_rep() {
  local reply
  reply=$(mktemp)
  if ui_step "First rep: $STARTER_NAME says hello" -- curl -sS --fail --max-time 240 -o "$reply" \
      -H "Authorization: Bearer $API_KEY" -H 'content-type: application/json' \
      -d "{\"model\": \"$STARTER_NAME\", \"max_tokens\": 60, \"chat_template_kwargs\": {\"enable_thinking\": false},
           \"messages\": [{\"role\": \"user\", \"content\": \"Welcome me to my new openWeightGym in one short, upbeat sentence.\"}]}" \
      http://127.0.0.1:8442/v1/chat/completions; then
    FIRST_WORDS=$("$DIR/.venv/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1]))["choices"][0]["message"]["content"].strip().splitlines()[0][:70])' "$reply" 2>/dev/null || true)
    # shellcheck disable=SC1111  # the quotation marks are meant
    [[ -n $FIRST_WORDS ]] && ui_note "“$FIRST_WORDS”"
  else
    ui_warn "The model did not answer yet" "it may still be loading; try Chat in the console"
  fi
  rm -f "$reply"
}

# ---- the finish -----------------------------------------------------------------------------

finale() {
  ui_finale "Set complete"
  local lines=() console_local=http://localhost:8440 console_lan=http://$HOST_ADDRESS:8440
  case "$TOPOLOGY" in
    all|front)
      lines+=("${C_MUTED}Console     ${C_RESET}${C_GOLD}$console_local${C_RESET}")
      [[ $HOST_ADDRESS != 127.0.0.1 ]] && lines+=("${C_MUTED}            ${C_RESET}$console_lan  ${C_DIM}(other devices)${C_RESET}")
      lines+=("${C_MUTED}Sign in     ${C_RESET}$ADMIN${TEMPORARY:+  ${C_DIM}temporary password:${C_RESET} $TEMPORARY}")
      lines+=("${C_MUTED}API         ${C_RESET}http://$HOST_ADDRESS:8442/v1")
      [[ -n $API_KEY ]] && lines+=("${C_MUTED}API key     ${C_RESET}$API_KEY  ${C_DIM}(shown once)${C_RESET}")
      ((STARTER)) && [[ $TOPOLOGY == all ]] && lines+=("${C_MUTED}Model       ${C_RESET}$STARTER_NAME")
      if [[ $TOPOLOGY == front ]]; then
        lines+=("" "${C_BOLD}Add a GPU node${C_RESET}: run ${C_GOLD}owg pair${C_RESET} here, or open Settings → Compute nodes,")
        lines+=("and run the command it gives on the GPU machine.")
      fi ;;
    compute)
      lines+=("${C_MUTED}This node   ${C_RESET}http://$HOST_ADDRESS:8442${MACHINE_NAME:+  ${C_DIM}($MACHINE_NAME)${C_RESET}}")
      lines+=("${C_MUTED}Console     ${C_RESET}${JOIN_URL:-$FRONT_URL}")
      if ((PAIRED)); then
        lines+=("" "The console has added this node: it is under Settings → Compute nodes.")
      else
        lines+=("" "On the console machine, put this in settings/weightroom.toml:")
        lines+=("  ${C_GOLD}compute = \"http://$HOST_ADDRESS:8442\"${C_RESET}")
        lines+=("then: owg restart turnstile weightroom")
      fi ;;
  esac
  lines+=("" "${C_GOLD}owg${C_RESET} status · logs · update · doctor · uninstall")
  if ((SYSTEM)); then lines+=("${C_DIM}Installed in $DIR, run by the $SVC account · log: ${OWG_LOG/#$HOME/\~}${C_RESET}")
  else lines+=("${C_DIM}Installed in ${DIR/#$HOME/\~} · log: ${OWG_LOG/#$HOME/\~}${C_RESET}"); fi
  ui_box "You're in" "${lines[@]}"
  if ((BROWSER && !ASSUME_YES)) && [[ $TOPOLOGY != compute ]]; then ui_open_browser "$console_local" || true; fi
  printf '\n'
}

main "$@"
