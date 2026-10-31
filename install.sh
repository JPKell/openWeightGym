#!/usr/bin/env bash
# Install openWeightGym: your own models behind a web console, an OpenAI-compatible API, tools
# and voice. Run it either way:
#
#   curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/install.sh | bash
#   ./install.sh               from a checkout, which then becomes the installation
#
# It checks the machine, asks a few questions (Enter takes the defaults), then downloads what is
# needed, writes settings for this machine's part, starts the services and checks they answer.
# Running it again on an installed machine offers to update or repair. `--help` lists options
# for unattended installs; with `curl | bash`, pass them after `bash -s --`.
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

usage() {
  cat <<'EOF'
Usage: install.sh [options]        (curl ... | bash -s -- [options])

  --dir PATH              install here (default: ~/openWeightGym, or this checkout)
  --topology NAME         all (one machine), front (console only) or compute (GPU node)
  --yes                   take the defaults without asking
  --admin NAME            the first account (its password from OWG_ADMIN_PASSWORD, or asked)
  --front-url URL         GPU node: the console's turnstile, such as http://192.168.1.10:8442
  --machine-key-file F    GPU node: a copy of the console's keys/machine.key
  --compute-url URL       console: the GPU node's LoadCoach, such as http://192.168.1.20:8442
  --llama CHOICE          auto, cuda-13.4, cuda-12.8, rocm, vulkan, cpu, source, skip, or the
                          path of a llama-server you already have (default: auto)
  --database NAME         sqlite (default) or postgresql (installed and set up locally)
  --voice                 also install speech recognition and voices
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
  mkdir -p "$(dirname "$OWG_LOG")"
  : >"$OWG_LOG"
  export OWG_LOG
  trap 'ui_cleanup; on_error $LINENO' ERR
  trap 'ui_cleanup' EXIT
  trap 'ui_cleanup; printf "\n"; ui_fail "Stopped. Nothing finished is undone; run the installer again to carry on."; exit 130' INT

  if [[ $ACTION == uninstall ]]; then run_uninstaller; fi
  ui_banner "$(installed_version)"
  warm_up
  if is_installed "$DIR"; then
    case "$ACTION" in
      update) update ;;
      repair) reinstall ;;
      *) installed_menu ;;
    esac
    return 0
  fi
  [[ -z $ACTION ]] || ui_die "openWeightGym is not installed in $DIR." "Install it first, or give its folder with --dir."
  game_plan
  account
  loading_plates
  setting_up
  finishing
  spotting
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
  DIR=${OWG_HOME:-} TOPOLOGY='' ASSUME_YES=0 ADMIN='' FRONT_URL='' MACHINE_KEY_FILE='' COMPUTE_URL=''
  LLAMA=auto DATABASE=sqlite VOICE=0 STARTER=1 LINGER_WANTED=1 BROWSER=1 ACTION='' PASSTHROUGH=()
  while (($#)); do
    case "$1" in
      --dir) DIR=${2:?--dir needs a folder}; shift 2 ;;
      --topology) TOPOLOGY=${2:?--topology needs all, front or compute}; shift 2 ;;
      --yes|--express) ASSUME_YES=1; shift ;;
      --admin) ADMIN=${2:?--admin needs a name}; shift 2 ;;
      --front-url) FRONT_URL=${2:?--front-url needs a URL}; shift 2 ;;
      --machine-key-file) MACHINE_KEY_FILE=${2:?--machine-key-file needs a file}; shift 2 ;;
      --compute-url) COMPUTE_URL=${2:?--compute-url needs a URL}; shift 2 ;;
      --llama) LLAMA=${2:?--llama needs a choice}; shift 2 ;;
      --database) DATABASE=${2:?--database needs sqlite or postgresql}; shift 2 ;;
      --voice) VOICE=1; shift ;;
      --no-model) STARTER=0; shift ;;
      --no-linger) LINGER_WANTED=0; shift ;;
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
  ((ASSUME_YES)) && export OWG_DEFAULTS=1
  return 0
}

# The look and the machine checks come from the checkout beside this file or, under `curl | bash`,
# from the same branch on GitHub.
load_ui() {
  CHECKOUT=''
  local self=${BASH_SOURCE[0]:-}
  if [[ -n $self && -f $self && -f $(dirname "$self")/scripts/lib/ui.sh ]]; then
    CHECKOUT=$(cd -- "$(dirname -- "$self")" && pwd)
    # shellcheck source=scripts/lib/ui.sh
    source "$CHECKOUT/scripts/lib/ui.sh"
    # shellcheck source=scripts/lib/detect.sh
    source "$CHECKOUT/scripts/lib/detect.sh"
  else
    command -v curl >/dev/null || { echo "install.sh needs curl." >&2; exit 1; }
    BOOT=$(mktemp -d)
    local part
    for part in ui detect; do
      curl -fsSL --retry 2 "$OWG_RAW/$OWG_REF/scripts/lib/$part.sh" -o "$BOOT/$part.sh" ||
        { echo "Could not fetch openWeightGym's installer from $OWG_RAW ($OWG_REF). Check the network and try again." >&2; exit 1; }
      # shellcheck disable=SC1090
      source "$BOOT/$part.sh"
    done
    rm -rf "$BOOT"
  fi
  if [[ -z $DIR ]]; then DIR=${CHECKOUT:-$HOME/openWeightGym}; fi
  DIR=$(realpath -m -- "$DIR")
}

installed_version() {
  local file=${CHECKOUT:-$DIR}/pyproject.toml
  if [[ -f $file ]]; then sed -n 's/^version = "\(.*\)"/\1/p' "$file" | head -1; else printf '%s' "$OWG_VERSION"; fi
}

is_installed() {
  [[ -x $1/.venv/bin/python ]] && compgen -G "$1/settings/*.toml" >/dev/null
}

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

  if ((EUID == 0)); then
    ui_warn "Running as root" "the services will run as root; an ordinary account is safer"
  fi

  MISSING_TOOLS=()
  local tool
  for tool in git tar; do command -v "$tool" >/dev/null || MISSING_TOOLS+=("$tool"); done
  if ((${#MISSING_TOOLS[@]})); then ui_warn "Not installed: ${MISSING_TOOLS[*]}" "they will be installed with ${PKG_MANAGER:-your package manager}"
  else ui_ok "git, curl and tar"; fi

  if ((SYSTEMD_USER)); then
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

  if command -v uv >/dev/null; then ui_ok "uv $(uv --version 2>/dev/null | awk '{print $2}')" "for Python 3.13"
  else ui_ok "Python 3.13" "fetched with uv into the installation; the system's Python is left alone"; fi

  if ((DISK_FREE_MB < 3000)); then
    ui_die "Only $((DISK_FREE_MB / 1024)) GB free where $DIR would go." "openWeightGym needs about 3 GB, and models need more. Choose another place with --dir."
  elif ((DISK_FREE_MB < 8000)); then
    ui_warn "$((DISK_FREE_MB / 1024)) GB free" "enough to start; models need more"
  else
    ui_ok "$((DISK_FREE_MB / 1024)) GB free" "for $DIR"
  fi

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
  actions+=(uninstall quit)
  labels+=("Uninstall|remove openWeightGym, choosing what to keep" "Quit|change nothing")
  ui_choose choice "What would you like to do?" 0 "${labels[@]}"
  case "${actions[choice]}" in
    update) update ;;
    repair) reinstall ;;
    uninstall) run_uninstaller ;;
    *) ui_info "Nothing changed." ;;
  esac
}

update() {
  if [[ -d $DIR/.git ]]; then
    if [[ -n $(git -C "$DIR" status --porcelain --untracked-files=no) ]]; then
      ui_die "$DIR has changes of your own." "Commit or stash them, then update again."
    fi
    local before
    before=$(git -C "$DIR" rev-parse --short HEAD)
    ui_step "Fetching the latest openWeightGym" -- git -C "$DIR" pull --ff-only ||
      ui_die "Could not fetch the latest openWeightGym." "The log says why: $OWG_LOG"
    if [[ $(git -C "$DIR" rev-parse --short HEAD) == "$before" ]]; then ui_ok "Already up to date" "$before"
    else ui_ok "Updated" "$before → $(git -C "$DIR" rev-parse --short HEAD)"; fi
  else
    ui_warn "$DIR is not a git checkout" "reinstalling the code that is there"
  fi
  reinstall
}

# The uninstaller beside this installer, or the one on GitHub under `curl | bash`.
run_uninstaller() {
  local script=${CHECKOUT:-$DIR}/uninstall.sh
  if [[ -x $script ]]; then exec "$script" --dir "$DIR" "${PASSTHROUGH[@]}"; fi
  exec bash -c 'curl -fsSL "$1" | bash -s -- "${@:2}"' _ "$OWG_RAW/$OWG_REF/uninstall.sh" --dir "$DIR" "${PASSTHROUGH[@]}"
}

reinstall() {
  find_uv
  ui_section "Setting up" "Packages, settings and services"
  "$DIR/scripts/install-suite.sh" --root "$DIR" --embedded --non-interactive --yes --database keep --uv "$UV" ||
    ui_die "Reinstalling did not finish." "The log says why: $OWG_LOG"
  link_owg
  ui_section "Spotting" "Checking it all works"
  check_services
  ui_finale "Back in shape"
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
}

valid_host() {
  [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] && return 0
  ui_warn "That is not an address: use an IP address such as 192.168.1.10, or a host name"
  return 1
}
valid_host_or_empty() { [[ -z $1 ]] || valid_host "$1"; }

ask_console() {
  if [[ -z $FRONT_URL ]]; then
    local host=''
    ((ASSUME_YES)) && ui_die "A GPU node needs --front-url, the console's address."
    ui_input host "The console machine's address (its IP or host name)" "" valid_host
    FRONT_URL=http://$host:8442
  fi
  if [[ -n $MACHINE_KEY_FILE ]]; then
    [[ -r $MACHINE_KEY_FILE ]] || ui_die "Cannot read $MACHINE_KEY_FILE."
    MACHINE_KEY=$(<"$MACHINE_KEY_FILE")
  elif [[ -f $DIR/keys/machine.key ]]; then
    MACHINE_KEY=$(<"$DIR/keys/machine.key")
  else
    ((ASSUME_YES)) && ui_die "A GPU node needs --machine-key-file, a copy of the console's keys/machine.key."
    ui_note "The console and this node sign what they send each other with one shared key."
    ui_note "On the console, show it with:  cat ~/openWeightGym/keys/machine.key"
    ui_secret MACHINE_KEY "Paste the console's machine key" 20 once
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
    compute) where="GPU node for ${FRONT_URL#http://}" ;;
  esac
  [[ $LLAMA == auto ]] && engine=$LLAMA_BACKEND || engine=$LLAMA
  lines+=("${C_MUTED}Setup        ${C_RESET}$where")
  lines+=("${C_MUTED}Install to   ${C_RESET}${DIR/#$HOME/\~}")
  lines+=("${C_MUTED}Programs     ${C_RESET}$(plain_programs)")
  lines+=("${C_MUTED}Data         ${C_RESET}$([[ $DATABASE == sqlite ]] && printf 'SQLite, in data/' || printf 'PostgreSQL, on this machine')")
  if [[ $TOPOLOGY != front ]]; then
    lines+=("${C_MUTED}Engine       ${C_RESET}llama.cpp · $(backend_label "$engine")")
    if ((STARTER)); then lines+=("${C_MUTED}First model  ${C_RESET}$STARTER_TITLE, about 1.8 GB")
    else lines+=("${C_MUTED}First model  ${C_RESET}none for now"); fi
  fi
  if ((SYSTEMD_USER)); then
    lines+=("${C_MUTED}Runs         ${C_RESET}now, and $( ((LINGER_WANTED)) && printf 'at every boot, signed in or not' || printf 'while you are signed in')")
  fi
  ui_box "Your setup" "${lines[@]}"
}

customize() {
  local choice=0 answer=''
  if [[ -z $CHECKOUT ]]; then
    ui_input answer "Install to" "$DIR"
    DIR=$(realpath -m -- "${answer/#\~/$HOME}")
  fi
  ui_confirm "Install speech recognition and voices too? (large; for the Voice pages)" n && VOICE=1 || VOICE=0
  ui_choose choice "Where should openWeightGym keep its data?" 0 \
    "SQLite|files in data/, nothing else to run" "PostgreSQL|a database server on this machine, set up for you"
  local -a databases=(sqlite postgresql)
  DATABASE=${databases[choice]}
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
  fi
  if ((SYSTEMD_USER)); then
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
  fetch_code
  find_uv
  ui_step "Python 3.13" -- "$UV" python install --no-bin 3.13 ||
    ui_die "uv could not fetch Python 3.13." "Check the network; the log has uv's own words."
  [[ $TOPOLOGY == front ]] || engine
  if ((STARTER)); then
    local model=$DIR/models/llm/$STARTER_FILE
    if [[ -s $model ]]; then ui_ok "$STARTER_TITLE is already here"
    else ui_download "$STARTER_URL" "$model" "$STARTER_TITLE" || { ui_warn "Skipping the starter model" "add models later from the console"; STARTER=0; }
    fi
  fi
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

fetch_code() {
  if [[ -n $CHECKOUT ]]; then
    ui_ok "openWeightGym $(installed_version)" "this checkout, ${DIR/#$HOME/\~}"
    CREATED_DIR=0
    return 0
  fi
  if [[ -e $DIR ]] && [[ -n $(ls -A "$DIR" 2>/dev/null) ]] && [[ ! -f $DIR/pyproject.toml && -f $DIR/data/install-receipt ]]; then
    # What an earlier uninstall kept: settings, keys, data or models. Put the code back around them.
    ui_info "Found what an earlier installation kept in ${DIR/#$HOME/\~}" "it is used as it is"
    local clone=$DIR/.owg-clone
    rm -rf "$clone"
    ui_step "Fetching openWeightGym ($OWG_REF)" -- git clone --quiet --no-checkout --branch "$OWG_REF" "$OWG_REPO" "$clone" ||
      ui_die "Could not fetch openWeightGym from $OWG_REPO."
    mv "$clone/.git" "$DIR/.git"
    rmdir "$clone"
    git -C "$DIR" reset --quiet --hard
    CREATED_DIR=1
  elif [[ -e $DIR ]] && [[ -n $(ls -A "$DIR" 2>/dev/null) ]]; then
    [[ -f $DIR/pyproject.toml && -f $DIR/scripts/install-suite.sh ]] ||
      ui_die "$DIR exists and is not openWeightGym." "Choose another place with --dir, or empty that folder."
    ui_ok "openWeightGym is already in ${DIR/#$HOME/\~}"
    CREATED_DIR=0
  else
    mkdir -p "$(dirname "$DIR")"
    ui_step "Fetching openWeightGym ($OWG_REF)" -- git clone --quiet --branch "$OWG_REF" "$OWG_REPO" "$DIR" ||
      ui_die "Could not fetch openWeightGym from $OWG_REPO."
    CREATED_DIR=1
  fi
  CHECKOUT=$DIR
}

# uv makes the Python environment. One already on this machine is used; otherwise a pinned copy
# goes in the installation's .tools folder. The Python it fetches lives in .python, beside it.
find_uv() {
  export UV_PYTHON_INSTALL_DIR=$DIR/.python
  if [[ -x $DIR/.tools/uv ]]; then UV=$DIR/.tools/uv; return 0; fi
  if command -v uv >/dev/null; then UV=$(command -v uv); return 0; fi
  local arch=$OS_ARCH work
  [[ $arch == arm64 ]] && arch=aarch64
  work=$(mktemp -d)
  ui_download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-unknown-linux-gnu.tar.gz" \
    "$work/uv.tar.gz" "uv $UV_VERSION" || ui_die "Could not download uv."
  mkdir -p "$DIR/.tools"
  tar -xzf "$work/uv.tar.gz" -C "$DIR/.tools" --strip-components=1
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
  local options=(--prefix "$DIR/runtimes/llamacpp")
  ((ASSUME_YES)) && options+=(--yes)
  case "$LLAMA" in
    auto) ;;
    source) options+=(--from-source) ;;
    *) options+=(--backend "$LLAMA") ;;
  esac
  if ! "$DIR/scripts/install-llamacpp.sh" "${options[@]}"; then
    ui_warn "llama.cpp did not install" "the rest goes on; run scripts/install-llamacpp.sh to try again"
    STARTER_REGISTER=0
    return 0
  fi
  LLAMA_SERVER=$DIR/runtimes/llamacpp/current/llama-server
}

# ---- setting up -----------------------------------------------------------------------------

setting_up() {
  ui_section "Setting up" "Python, settings and services"
  services_for
  if [[ $TOPOLOGY == compute && -n ${MACHINE_KEY:-} && ! -f $DIR/keys/machine.key ]]; then
    mkdir -p "$DIR/keys"
    chmod 700 "$DIR/keys"
    (umask 077 && printf '%s\n' "$MACHINE_KEY" >"$DIR/keys/machine.key")
    ui_ok "Saved the console's machine key"
  fi
  local options=(--root "$DIR" --embedded --non-interactive --yes --services "$SERVICES" --topology "$TOPOLOGY"
                 --address "$HOST_ADDRESS" --start-new --uv "$UV" --database "$DATABASE")
  [[ -n $FRONT_URL ]] && options+=(--front-url "$FRONT_URL")
  [[ -n $COMPUTE_URL ]] && options+=(--compute-url "$COMPUTE_URL")
  [[ -n ${LLAMA_SERVER:-} ]] && options+=(--llama-server "$LLAMA_SERVER")
  [[ $DATABASE == postgresql ]] && options+=(--install-postgresql --provision-postgresql)
  if [[ $DATABASE == postgresql ]]; then ui_sudo "installing and setting up PostgreSQL"; fi
  "$DIR/scripts/install-suite.sh" "${options[@]}" || ui_die "Setting up did not finish." "The log says why: $OWG_LOG"
}

finishing() {
  local weightroom=("$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml")
  local loadcoach=("$DIR/.venv/bin/loadcoach" --config "$DIR/settings/loadcoach.toml")
  API_KEY='' TEMPORARY=''
  if [[ $TOPOLOGY != compute ]]; then
    if "${weightroom[@]}" user list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx "$ADMIN"; then
      ui_ok "Account $ADMIN is already there"
    else
      local said
      said=$("${weightroom[@]}" user add "$ADMIN" superuser 2>>"$OWG_LOG") || ui_die "Could not add the account $ADMIN."
      API_KEY=$(printf '%s\n' "$said" | grep -oE 'lc_[A-Za-z0-9_-]+' | tail -1)
      if [[ -n $PASSWORD ]]; then
        printf '%s\n' "$PASSWORD" | "${weightroom[@]}" user password "$ADMIN" --stdin >>"$OWG_LOG" 2>&1 ||
          ui_die "Could not set $ADMIN's password."
      else
        TEMPORARY=$("${weightroom[@]}" user temporary "$ADMIN" 2>>"$OWG_LOG" | tail -1)
      fi
      ui_ok "Made the account $ADMIN"
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
  if ((SYSTEMD_USER && LINGER_WANTED && !LINGER)); then
    if loginctl enable-linger "${USER:-$(id -un)}" 2>>"$OWG_LOG"; then
      ui_ok "Services keep running when you log out"
      LINGER_ENABLED=1
    else
      ui_warn "Could not keep the services running after logout" "run: sudo loginctl enable-linger ${USER:-\$USER}"
    fi
  fi
  link_owg
  write_receipt
}

# The owg command, on this account's PATH, and a note of where the installation is.
link_owg() {
  OWG_LINK=$HOME/.local/bin/owg
  mkdir -p "$(dirname "$OWG_LINK")" "${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym"
  ln -sfn "$DIR/scripts/owg" "$OWG_LINK"
  printf '%s\n' "$DIR" >"${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ui_ok "The owg command" "owg status, owg logs, owg update, owg uninstall" ;;
    *) ui_ok "The owg command" "in ~/.local/bin, which is not on your PATH yet: open a new terminal, or run ~/.local/bin/owg" ;;
  esac
}

# What this installer made, for the uninstaller: it removes only what is listed here.
write_receipt() {
  local receipt=$DIR/data/install-receipt
  mkdir -p "$DIR/data"
  {
    echo "# What install.sh made here, read by the uninstaller. Only what is listed is removed."
    echo "version=1"
    echo "installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "mode=user"
    echo "dir=$DIR"
    echo "created_dir=${CREATED_DIR:-0}"
    echo "topology=$TOPOLOGY"
    echo "services=$SERVICES"
    echo "unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    echo "python=$DIR/.python"
    echo "venv=$DIR/.venv"
    [[ ${UV_DOWNLOADED:-0} == 1 ]] && echo "uv=$DIR/.tools"
    [[ -d $DIR/runtimes/llamacpp ]] && echo "runtime=$DIR/runtimes/llamacpp"
    [[ -d $DIR/models ]] && echo "models=$DIR/models"
    echo "linger_enabled=${LINGER_ENABLED:-0}"
    echo "owg_link=$OWG_LINK"
    echo "pointer=${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install"
    echo "database=$DATABASE"
    [[ -n ${ADMIN:-} ]] && echo "admin=$ADMIN"
  } >"$receipt.new"
  mv "$receipt.new" "$receipt"
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
  local service
  if ! ((SYSTEMD_USER)); then
    ui_info "Start the services by hand: see docs/README.md"
    return 0
  fi
  services_from_receipt
  for service in ${SERVICES//,/ }; do
    if systemctl --user is-active --quiet "$service.service"; then ui_ok "$service is running"
    else ui_fail "$service is not running" "journalctl --user -u $service -n 30"; fi
  done
}

services_from_receipt() {
  [[ -n ${SERVICES:-} ]] && return 0
  SERVICES=$(sed -n 's/^services=//p' "$DIR/data/install-receipt" 2>/dev/null || true)
  if [[ -z $SERVICES ]]; then
    local unit found=()
    for unit in loadcoach toollocker spotter voice weightroom turnstile voice-gateway; do
      [[ -f ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit.service ]] && found+=("$unit")
    done
    SERVICES=$(IFS=,; printf '%s' "${found[*]}")
  fi
  return 0
}

spotting() {
  ui_section "Spotting" "Checking it all works"
  ((SYSTEMD_USER)) || { ui_info "systemd is not running here, so nothing was started"; return 0; }
  check_services
  local key
  case "$TOPOLOGY" in
    all|front)
      ui_step "The console answers" -- wait_http http://127.0.0.1:8440/ 60 || true
      key=$(<"$DIR/keys/weightroom.key")
      if [[ $TOPOLOGY == all ]]; then
        ui_step "The API answers through the gateway" -- wait_http http://127.0.0.1:8442/v1/models 60 -H "Authorization: Bearer $key" || true
      fi ;;
    compute)
      ui_step "LoadCoach answers on $HOST_ADDRESS" -- wait_http "http://$HOST_ADDRESS:8442/v1/models" 60 || true ;;
  esac
  if [[ $TOPOLOGY == all ]] && ((STARTER)) && [[ -n $API_KEY ]]; then first_rep; fi
}

# The first request: load the starter model and ask it something.
first_rep() {
  local reply=$DIR/data/.first-rep.json
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
        lines+=("" "${C_BOLD}Add a GPU node${C_RESET}: run the installer there, choose GPU node, and give it")
        lines+=("this address ($HOST_ADDRESS) and the key in ${DIR/#$HOME/\~}/keys/machine.key.")
      fi ;;
    compute)
      lines+=("${C_MUTED}This node   ${C_RESET}http://$HOST_ADDRESS:8442")
      lines+=("${C_MUTED}Console     ${C_RESET}$FRONT_URL")
      lines+=("" "On the console machine, put this in settings/weightroom.toml:")
      lines+=("  ${C_GOLD}compute = \"http://$HOST_ADDRESS:8442\"${C_RESET}")
      lines+=("then: systemctl --user restart turnstile weightroom") ;;
  esac
  lines+=("" "${C_GOLD}owg${C_RESET} status · logs · update · doctor · uninstall")
  lines+=("${C_DIM}Installed in ${DIR/#$HOME/\~} · log: ${OWG_LOG/#$HOME/\~}${C_RESET}")
  ui_box "You're in" "${lines[@]}"
  if ((BROWSER && !ASSUME_YES)) && [[ $TOPOLOGY != compute ]]; then ui_open_browser "$console_local" || true; fi
  printf '\n'
}

main "$@"
