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
  --components            add omitted components to an existing installation
  --discard-install       remove partial engine builds and cancel the saved install plan
  --services LIST         components: add comma-separated service names
  --strata install|skip|keep     optional Strata CUDA runtime (default: keep)
  --strata-host-ram-mib N        Strata host RAM budget (default: 22528 MiB)
  --sdcpp install|skip|keep      optional stable-diffusion.cpp runtime
  --sdcpp-backend cuda|vulkan|cpu  image backend (default: cuda)
  --audiocpp install|skip|keep   optional audio.cpp runtime
  --whispercpp install|skip|keep   optional whisper.cpp runtime
  --audiocpp-backend cuda|vulkan|cpu  audio backend (default: cuda)
  --whispercpp-backend cuda|vulkan|cpu  audio backend (default: cuda)
  --classifier-tools install|skip|keep  optional CPU classifier conversion tools
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
  --platform-services LIST  initial console services: images,speech,audio,downloads,classifiers or none
  --voice-key-file FILE   manual GPU-node setup: the console's voice-service.key (pairing supplies it)
  --power-control         let the console put this machine to sleep, restart or switch it off
  --no-linger             let the services stop when you log out
  --no-browser            do not open the console in a browser at the end
  --ref BRANCH            which branch of openWeightGym to fetch (default: main)
  --plain                 no colour or animation
  --update                fetch code and select it for the next restart (as owg update does)
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
  ui_log_init || ui_die "Cannot create a private installer log at $OWG_LOG." \
    "Check this account can write to $(dirname "$OWG_LOG"), or name another log: OWG_LOG=/tmp/owg-install.log"
  export OWG_LOG
  if [[ -n $ARGUMENT_ERROR ]]; then
    OWG_STOP_TITLE='Nothing was changed' ui_die --status 2 "$ARGUMENT_ERROR" "${ARGUMENT_STEPS[@]}" \
      'See every option and what it takes: run the same command with --help'
  fi
  trap 'on_error $? "${BASH_SOURCE[0]:-}" $LINENO "$BASH_COMMAND"' ERR
  trap 'ui_cleanup' EXIT
  trap 'ui_cleanup; printf "\n"; ui_fail "Stopped. Nothing finished is undone; run the installer again to carry on."; exit 130' INT

  locate_installation
  set_retry
  if [[ $ACTION == uninstall ]]; then run_uninstaller; fi
  ui_banner "$(installed_version)"
  if [[ $ACTION == discard-install ]]; then
    discard_install
    return 0
  fi
  # Updating code is independent of a saved installation plan, even if setup never finished.
  if [[ $ACTION == update ]]; then
    ((SYSTEM)) && ui_sudo "updating the system installation's code"
    update
    return 0
  fi
  RECOVERY_ACTION='continue'
  if [[ $(receipt_get state) == pending ]]; then
    interrupted_menu
    [[ $RECOVERY_ACTION != quit ]] || return 0
  fi
  warm_up
  if [[ $(receipt_get state) == pending && $RECOVERY_ACTION != restart ]]; then
    resume_settings
    if [[ $(receipt_get operation) == components ]]; then
      SERVICES=$(receipt_get services)
      apply_components
      return 0
    fi
    RESUMING_INSTALL=1
    ui_info "Resuming the confirmed installation plan" "settings and existing accounts are kept"
  elif [[ $RECOVERY_ACTION != restart ]] && is_installed "$DIR"; then
    ((SYSTEM)) && ui_sudo "looking after the system installation"
    case "$ACTION" in
      repair) reinstall ;;
      components) install_components ;;
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

on_error() {  # on_error STATUS FILE LINE COMMAND, from the ERR trap, which reads $? before anything else
  local status=$1 file=install.sh line=$3 command=$4
  ((BASH_SUBSHELL == 0)) || return 0  # a failure inside $(...) or a step is reported where it is used
  # Piped through bash, this script itself has no file name; its sourced parts do.
  [[ $2 == */* ]] && file=${2##*/}
  trap - ERR
  ui_cleanup
  ((status)) || status=1
  ui_die --status "$status" "The installer stopped unexpectedly, at $file line $line (exit status $status), running:
  $command" \
    "The lines just above it may say why: put that right" \
    "If it happens again, keep the log for whoever looks into it"
}

# How to carry on after a failure, the last step every failure lists. The helpers this runs
# (setting up, the engine, the uninstaller) list it too, so these are exported.
set_retry() {
  local command flags=''
  if [[ -f $DIR/install.sh ]]; then
    command=$(shell_path "$DIR/install.sh")
    [[ $ACTION != update ]] || command+=' --update'
    [[ $ACTION != discard-install ]] || command+=' --discard-install'
  else
    command="curl -fsSL $OWG_RAW/$OWG_REF/install.sh | bash"
    if ((SYSTEM)); then flags+=' --system'; fi
    if ((DIR_GIVEN)); then flags+=" --dir $(shell_path "$DIR")"; fi
    [[ $ACTION != update ]] || flags+=' --update'
    [[ $ACTION != discard-install ]] || flags+=' --discard-install'
    if [[ -n $flags ]]; then command+=" -s --$flags"; fi
  fi
  OWG_RETRY_COMMAND=$command
  OWG_RETRY="Run the installer again: $command
It carries on where it stopped; nothing finished is undone."
  case "$ACTION" in
    uninstall) OWG_STOP_TITLE="Uninstalling stopped" OWG_RETRY='' ;;
    update) OWG_STOP_TITLE="The update stopped"; OWG_RETRY="Run the code update again: $command" ;;
    discard-install) OWG_STOP_TITLE="Discarding the failed install stopped"; OWG_RETRY="Retry cleanup: $command" ;;
    *) OWG_STOP_TITLE="The install stopped" ;;
  esac
  export OWG_RETRY OWG_RETRY_COMMAND OWG_STOP_TITLE
}

# ---- arguments ------------------------------------------------------------------------------

parse_arguments() {
  DIR=${OWG_HOME:-} DIR_GIVEN=0 SYSTEM=0 TOPOLOGY='' ASSUME_YES=0 ADMIN='' FRONT_URL='' MACHINE_KEY_FILE=''
  COMPUTE_URL='' LLAMA=auto DATABASE=sqlite VOICE=0 LINGER_WANTED=1 BROWSER=1 POWER_RULE=0
  ACTION='' PASSTHROUGH=() JOIN_URL='' PAIR_CODE='' PAIRED=0 MACHINE_NAME=''
  VOICE_KEY_FILE='' VOICE_KEY=''
  PLATFORM_SERVICES=images,speech,audio,downloads,classifiers
  ADD_SERVICES='' STRATA_ACTION=keep STRATA_HOST_RAM_MIB=22528
  SDCPP_ACTION=keep SDCPP_BACKEND=cuda AUDIOCPP_ACTION=keep AUDIOCPP_BACKEND=cuda WHISPERCPP_ACTION=keep WHISPERCPP_BACKEND=cuda
  CLASSIFIER_TOOLS_ACTION=keep
  OPERATION=install
  RESUMING_INSTALL=0
  # A mistake in the options is told once the screen is set up (main), as loudly as any other stop.
  ARGUMENT_ERROR='' ARGUMENT_STEPS=()
  declare -gA PLAN_GIVEN=()
  local -A takes=([--dir]='a folder' [--topology]='all, front or compute' [--admin]='a name' [--front-url]='a URL'
    [--machine-key-file]='a file' [--compute-url]='a URL' [--join]='the console address' [--code]='the pairing code'
    [--llama]='a choice' [--database]='sqlite or postgresql' [--voice-key-file]='a file' [--ref]='a branch'
    [--services]='service names' [--strata]='install, skip or keep' [--strata-host-ram-mib]='a positive MiB budget'
    [--sdcpp]='install, skip or keep' [--sdcpp-backend]='cuda, vulkan or cpu'
    [--audiocpp]='install, skip or keep' [--audiocpp-backend]='cuda, vulkan or cpu'
    [--whispercpp]='install, skip or keep' [--whispercpp-backend]='cuda, vulkan or cpu'
    [--classifier-tools]='install, skip or keep'
    [--platform-services]='images,speech,audio,downloads,classifiers or none')
  [[ -n $DIR ]] && DIR_GIVEN=1
  while (($#)); do
    if [[ -v takes[$1] ]] && { (($# < 2)) || [[ -z $2 ]]; }; then
      ARGUMENT_ERROR="$1 needs ${takes[$1]}."
      return 0
    fi
    case "$1" in
      --dir) DIR=$2; DIR_GIVEN=1; shift 2 ;;
      --system) SYSTEM=1; shift ;;
      --topology) TOPOLOGY=$2; PLAN_GIVEN[TOPOLOGY]=1; shift 2 ;;
      --yes|--express) ASSUME_YES=1; shift ;;
      --admin) ADMIN=$2; PLAN_GIVEN[ADMIN]=1; shift 2 ;;
      --front-url) FRONT_URL=$2; PLAN_GIVEN[FRONT_URL]=1; shift 2 ;;
      --machine-key-file) MACHINE_KEY_FILE=$2; shift 2 ;;
      --compute-url) COMPUTE_URL=$2; PLAN_GIVEN[COMPUTE_URL]=1; shift 2 ;;
      --join) JOIN_URL=$2; TOPOLOGY=compute; PLAN_GIVEN[JOIN_URL]=1; PLAN_GIVEN[TOPOLOGY]=1; shift 2 ;;
      --code) PAIR_CODE=$2; shift 2 ;;
      --llama) LLAMA=$2; PLAN_GIVEN[LLAMA]=1; shift 2 ;;
      --database) DATABASE=$2; PLAN_GIVEN[DATABASE]=1; shift 2 ;;
      --voice) VOICE=1; PLAN_GIVEN[VOICE]=1; shift ;;
      --platform-services)
        PLATFORM_SERVICES=$2; PLAN_GIVEN[PLATFORM_SERVICES]=1
        if [[ ! $PLATFORM_SERVICES =~ ^(none|(images|speech|audio|downloads|classifiers)(,(images|speech|audio|downloads|classifiers))*)$ ]]; then
          ARGUMENT_ERROR='Platform services must be images,speech,audio,downloads,classifiers, a subset, or none.'; return 0
        fi
        shift 2 ;;
      --voice-key-file) VOICE_KEY_FILE=$2; shift 2 ;;
      --power-control) POWER_RULE=1; PLAN_GIVEN[POWER_RULE]=1; shift ;;
      --no-model) shift ;;  # no model is downloaded any more; still accepted so older commands work
      --no-linger) LINGER_WANTED=0; PLAN_GIVEN[LINGER_WANTED]=1; shift ;;
      --no-browser) BROWSER=0; shift ;;
      --ref) OWG_REF=$2; shift 2 ;;
      --plain) export OWG_PLAIN=1; PASSTHROUGH+=(--plain); shift ;;
      --update) ACTION=update; shift ;;
      --repair) ACTION=repair; shift ;;
      --components) ACTION=components; shift ;;
      --discard-install) ACTION=discard-install; shift ;;
      --services) ADD_SERVICES=$2; shift 2 ;;
      --strata) STRATA_ACTION=$2; PLAN_GIVEN[STRATA_ACTION]=1; shift 2 ;;
      --strata-host-ram-mib) STRATA_HOST_RAM_MIB=$2; PLAN_GIVEN[STRATA_HOST_RAM_MIB]=1; shift 2 ;;
      --sdcpp) SDCPP_ACTION=$2; PLAN_GIVEN[SDCPP_ACTION]=1; shift 2 ;;
      --sdcpp-backend) SDCPP_BACKEND=$2; PLAN_GIVEN[SDCPP_BACKEND]=1; shift 2 ;;
      --audiocpp) AUDIOCPP_ACTION=$2; PLAN_GIVEN[AUDIOCPP_ACTION]=1; shift 2 ;;
      --whispercpp) WHISPERCPP_ACTION=$2; PLAN_GIVEN[WHISPERCPP_ACTION]=1; shift 2 ;;
      --audiocpp-backend) AUDIOCPP_BACKEND=$2; PLAN_GIVEN[AUDIOCPP_BACKEND]=1; shift 2 ;;
      --whispercpp-backend) WHISPERCPP_BACKEND=$2; PLAN_GIVEN[WHISPERCPP_BACKEND]=1; shift 2 ;;
      --classifier-tools) CLASSIFIER_TOOLS_ACTION=$2; PLAN_GIVEN[CLASSIFIER_TOOLS_ACTION]=1; shift 2 ;;
      --uninstall) ACTION=uninstall; shift ;;
      --everything|--no-backup) PASSTHROUGH+=("$1"); shift ;;
      -h|--help) usage; exit 0 ;;
      *) ARGUMENT_ERROR="The installer has no option $1."; return 0 ;;
    esac
  done
  if [[ -n $TOPOLOGY && ! $TOPOLOGY =~ ^(all|front|compute)$ ]]; then
    ARGUMENT_ERROR="--topology is all, front or compute, not $TOPOLOGY."
  elif [[ ! $DATABASE =~ ^(sqlite|postgresql)$ ]]; then
    ARGUMENT_ERROR="--database is sqlite or postgresql, not $DATABASE."
  elif ((SYSTEM)) && [[ $DATABASE == postgresql ]]; then
    ARGUMENT_ERROR='A system installation keeps its data in SQLite for now.'
    ARGUMENT_STEPS=('Leave out --database postgresql, and move to PostgreSQL afterwards: docs/database-storage.md'
                    'Or install for yourself, without --system, to use PostgreSQL from the start')
  fi
  local variable
  for variable in STRATA_ACTION SDCPP_ACTION AUDIOCPP_ACTION WHISPERCPP_ACTION CLASSIFIER_TOOLS_ACTION; do
    [[ ${!variable} =~ ^(install|skip|keep)$ ]] || ARGUMENT_ERROR="${variable%_ACTION} must be install, skip or keep."
  done
  for variable in SDCPP_BACKEND AUDIOCPP_BACKEND WHISPERCPP_BACKEND; do
    [[ ${!variable} =~ ^(cuda|vulkan|cpu)$ ]] || ARGUMENT_ERROR="$variable must be cuda, vulkan or cpu."
  done
  [[ $STRATA_HOST_RAM_MIB =~ ^[1-9][0-9]{0,8}$ ]] || ARGUMENT_ERROR='--strata-host-ram-mib needs a positive whole number of MiB.'
  [[ -z $ADD_SERVICES || $ACTION == components ]] || ARGUMENT_ERROR='--services requires --components.'
  if [[ $TOPOLOGY == front && ( $STRATA_ACTION == install || $SDCPP_ACTION == install || $AUDIOCPP_ACTION == install || $WHISPERCPP_ACTION == install || $CLASSIFIER_TOOLS_ACTION == install ) ]]; then
    ARGUMENT_ERROR='Engines need a model runner: choose --topology all or compute.'
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
    command -v curl >/dev/null || boot_failed "The installer needs curl, and it is not installed." \
      "Install it: sudo apt-get install curl, sudo dnf install curl, sudo zypper install curl or sudo pacman -S curl"
    BOOT=$(mktemp -d)
    for part in ui detect receipt; do
      curl -fsSL --retry 2 "$OWG_RAW/$OWG_REF/scripts/lib/$part.sh" -o "$BOOT/$part.sh" ||
        boot_failed "The installer could not download its parts from $OWG_RAW (branch $OWG_REF)." \
          "Check this machine can reach GitHub: curl -sSI https://raw.githubusercontent.com | head -1" \
          "Behind a proxy? Tell the installer about it: export https_proxy=http://PROXY:PORT" \
          "If you named a branch with --ref, check it exists"
      # shellcheck disable=SC1090
      source "$BOOT/$part.sh"
    done
    rm -rf "$BOOT"
  fi
}

# A stop before the look is loaded, said as plainly and as loudly: boot_failed "What went wrong" STEP...
boot_failed() {
  local n=0 step
  printf '\n  FAILED: %s\n\n  What to do next:\n' "$1" >&2
  shift
  for step in "$@" 'Then run the installer again'; do n=$((n + 1)); printf '    %d. %s\n' "$n" "$step" >&2; done
  printf '\n' >&2
  exit 1
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
  [[ $(receipt_get state) != cancelled || -n $(receipt_get services) ]] || return 1
  receipt_present && return 0
  # Legacy deployments have no receipt. A virtualenv plus copied settings alone is incomplete.
  local unit command unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
  for unit in loadcoach weightroom turnstile toollocker spotter voice voice-gateway frontdesk tooldoor; do
    [[ -f $unit_dir/$unit.service ]] || continue
    command=$(sed -n 's/^ExecStart=//p' "$unit_dir/$unit.service" | head -1)
    command=${command//%h/$HOME}
    command=${command#\"}
    [[ $command == "$1"/.venv/bin/* ]] && return 0
  done
  return 1
}

# Restore a confirmed plan without saving passwords, API keys or the consumed pairing code.
resume_settings() {
  local pair variable key value
  for pair in TOPOLOGY:topology ADMIN:admin DATABASE:database LLAMA:llama VOICE:voice \
    LINGER_WANTED:linger_wanted POWER_RULE:power_rule FRONT_URL:front_url COMPUTE_URL:compute_url \
    MACHINE_NAME:machine_name JOIN_URL:join_url STRATA_ACTION:strata STRATA_HOST_RAM_MIB:strata_host_ram_mib \
    SDCPP_ACTION:sdcpp SDCPP_BACKEND:sdcpp_backend AUDIOCPP_ACTION:audiocpp AUDIOCPP_BACKEND:audiocpp_backend WHISPERCPP_ACTION:whispercpp WHISPERCPP_BACKEND:whispercpp_backend \
    CLASSIFIER_TOOLS_ACTION:classifier_tools \
    PLATFORM_SERVICES:platform_services; do
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
  core_tools
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
  for tool in git curl tar gzip age; do command -v "$tool" >/dev/null || MISSING_TOOLS+=("$tool"); done
  if ((${#MISSING_TOOLS[@]})); then ui_warn "Not installed: ${MISSING_TOOLS[*]}" "they will be installed with ${PKG_MANAGER:-your package manager}"
  else ui_ok "git, curl and tar"; fi

  if ((SYSTEM)); then
    ((SYSTEMD)) || ui_die "A system installation needs systemd running this machine." "Install for yourself instead, without --system."
    ui_ok "systemd" "services run as the $SVC account, from boot"
  elif ((SYSTEMD_USER)); then
    if ((LINGER)); then ui_ok "systemd user services" "kept running after you log out"
    else ui_ok "systemd user services"; fi
  else
    if ((OS_WSL)); then
      ui_attention "systemd is not running in this WSL system, so the services will be set up but not started." \
        "Switch it on: add these two lines to /etc/wsl.conf
[boot]
systemd=true" \
        "Restart WSL: run wsl --shutdown in Windows, then open this system again" \
        "Then start the services: owg start"
    else
      ui_attention "systemd user services are not running for this account, so the services will be set up but not started." \
        "Log in as $(id -un) directly, or over SSH, rather than switching to it with su or sudo -i: systemd starts then" \
        "Or keep them running without a log-in: sudo loginctl enable-linger $(id -un)" \
        "Then start the services: owg start"
    fi
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
  check_existing_units
}

# Units of the same names, or drop-ins that change what they run, would make the new services run
# someone else's code: stop before anything is changed, and say exactly how to clear the way.
check_existing_units() {
  local other unit list=''
  detect_units "$DIR"
  for unit in "${UNITS_MASKED[@]}"; do
    ui_warn "The $unit service is switched off (masked)" "if this installation uses it, it is switched back on"
  done
  ((${#UNITS_OTHER[@]} + ${#UNITS_DROPINS[@]})) || return 0
  units_retire_steps
  other=$UNITS_OTHER_ROOT
  if [[ -n $other && -f $other/.install-receipt && -x $other/install.sh ]]; then
    ui_die "openWeightGym is already installed for this account, in $other." \
      "To update or repair that one, run its installer: $(shell_path "$other/install.sh")" \
      "To install here instead, uninstall that one first: $(shell_path "$other/uninstall.sh")"
  fi
  if [[ -n $other ]]; then
    for unit in "${UNITS_OTHER[@]}"; do list+=", $unit"; done
    ui_die "Another openWeightGym (or AiSuite) installation, not made by this installer, already runs
services here from $(shell_path "$other"): ${list#, }.
New services with the same names would replace them." \
      "${UNITS_STEPS[@]}" \
      "Its folder, $(shell_path "$other"), with its settings and data, is not touched by any of this"
  fi
  list=''
  for unit in "${UNITS_DROPINS[@]}"; do list+=$'\n'"  $(shell_path "$unit")"; done
  ui_die "Leftover systemd drop-ins would change what openWeightGym's services run (their code,
libraries or command), whoever installed them:$list" "${UNITS_STEPS[@]}"
}

# ---- an existing installation ---------------------------------------------------------------

interrupted_menu() {
  # Explicit repair and unattended runs continue the saved plan; update is handled separately.
  [[ -z $ACTION ]] && ((!ASSUME_YES)) || return 0
  ui_section "Welcome back" "A previous installation in ${DIR/#$HOME/\~} did not finish"
  local choice=0 rc=0
  ui_choose choice "What would you like to do?" 0 \
    "Continue previous install|use the saved choices and keep existing settings and data" \
    "Uninstall components and start again|choose what to remove, then choose a new setup" \
    "Discard failed install|remove partial engine builds and cancel saved choices" \
    "Quit|change nothing"
  case "$choice" in
    0) ;;
    1)
      uninstall_components --for-reinstall || rc=$?
      case "$rc" in
        0) ;;
        3) RECOVERY_ACTION=quit; return 0 ;;
        *) ui_child_failed "$rc" "Uninstalling did not finish, so the installation was not restarted." ;;
      esac
      receipt_load "$DIR"
      CREATED_DIR=${RECEIPT[created_dir]:-0}
      # The uninstaller may have removed the checkout this installer was running from.
      [[ -f ${CHECKOUT:-$DIR}/pyproject.toml ]] || CHECKOUT=''
      if ! pwd -P >/dev/null 2>&1; then cd "$HOME"; fi
      RECOVERY_ACTION=restart
      ;;
    2) discard_install; RECOVERY_ACTION=quit ;;
    *) RECOVERY_ACTION=quit; ui_info "Nothing changed." ;;
  esac
}

discard_install() {
  [[ $(receipt_get state) == pending ]] || { ui_info 'No failed installation plan to discard.'; return 0; }
  ACTION=discard-install
  set_retry
  ((SYSTEM)) && ui_sudo 'discarding the system installation’s failed setup'
  local python=python3
  if as_owner test -x "$DIR/.venv/bin/python"; then python=$DIR/.venv/bin/python; fi
  local runner=(as_owner)
  if ((SYSTEM)); then
    # llama.cpp may have failed before handing its root-owned build back to the service account.
    owner_environment "$SVC"
    runner=(sudo -n /usr/bin/env "${OWNER_ENV[@]}")
  fi
  ui_section 'Discard failed install' 'Completed components, settings, models and build tools are kept'
  "${runner[@]}" "$python" "$DIR/scripts/discard-install.py" --root "$DIR" || return $?
  if ((!ASSUME_YES)); then
    ui_confirm 'Remove these partial builds and cancel all saved installation choices?' n || return 0
  fi
  "${runner[@]}" "$python" "$DIR/scripts/discard-install.py" --root "$DIR" --apply || return $?
  receipt_load "$DIR"
  ui_ok 'Failed installation discarded' 'It will not resume next run. Use owg install to select components again.'
}

installed_menu() {
  ui_section "Welcome back" "openWeightGym is installed in ${DIR/#$HOME/\~}"
  local choice=0
  local -a actions=(update repair components) labels=(
    "Update|fetch the latest openWeightGym code only"
    "Repair|reinstall packages and services as they are"
    "Install components|add services or model engines skipped earlier")
  if [[ ,$(receipt_get services), == *,weightroom,* ]]; then
    actions+=(pair)
    labels+=("Add a compute node|make a one-time code another machine joins with")
  fi
  actions+=(uninstall quit)
  labels+=("Uninstall|remove openWeightGym, choosing what to keep" "Quit|change nothing")
  ui_choose choice "What would you like to do?" 0 "${labels[@]}"
  case "${actions[choice]}" in
    update) update ;;
    repair) reinstall ;;
    components) install_components ;;
    pair) as_owner "$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml" pair ;;
    uninstall) run_uninstaller ;;
    *) ui_info "Nothing changed." ;;
  esac
}

update() {
  ACTION=update
  set_retry
  if [[ -e $DIR/.git ]]; then
    local git_dir
    git_dir="git -C $(shell_path "$DIR")"
    if [[ -n $(as_owner git -C "$DIR" status --porcelain --untracked-files=no) ]]; then
      ui_die "$DIR has changes of your own, and updating would mix them with the new code." \
        "See them: $git_dir status" "Keep them aside: $git_dir stash (or commit them)"
    fi
    local before
    before=$(as_owner git -C "$DIR" rev-parse --short HEAD)
    ui_step "Fetching the latest openWeightGym" -- as_owner git -C "$DIR" pull --ff-only ||
      ui_die "Could not fetch the latest openWeightGym." \
        "?The lines above say why: usually the network, or GitHub not answering" \
        "If git says the branches have diverged, see where yours stands: $git_dir status"
    if [[ $(as_owner git -C "$DIR" rev-parse --short HEAD) == "$before" ]]; then ui_ok "Already up to date" "$before"
    else ui_ok "Updated" "$before → $(as_owner git -C "$DIR" rev-parse --short HEAD)"; fi
  else
    ui_die "$DIR is not a Git checkout, so its code cannot be updated with git pull." \
      'Use an openWeightGym Git checkout for code updates'
  fi
  refresh_runtime_sources "$DIR" || ui_die 'Code was fetched, but service source overrides could not be updated.' \
    'Resolve the override error above, then run owg update again before restarting services'
  if [[ -f $DIR/engines/whisper_cpp/install.py ]]; then
    ui_step 'Updating installed whisper.cpp runtime' -- as_owner "$DIR/.venv/bin/python" \
      "$DIR/engines/whisper_cpp/install.py" --root "$DIR" --update ||
      ui_die 'Code was fetched, but the installed whisper.cpp runtime could not be updated.' 'Resolve the build error, then run owg update again'
  fi
  if [[ -f $DIR/settings/weightroom.toml ]]; then
    ui_step 'Building the Voice desktop download' -- as_owner "$DIR/.venv/bin/python" \
      "$DIR/scripts/package-voice-desktop.py" "$DIR/weightroom/static/LoadCoach-Voice.zip" ||
      ui_die 'Code was fetched, but the Voice desktop download could not be built.' \
        'Resolve the packaging error above, then run owg update again'
  fi
  refresh_component_versions "$DIR" || ui_die 'Code was fetched, but component versions could not be recorded.' \
    'Check the update log, then run owg restart to retry.'
  ui_info "${C_ERR}Code update complete. Restart running services when ready: owg restart${C_RESET}"
  if [[ $(receipt_get state) == pending ]]; then
    ui_note 'The unfinished installation is still saved. Run install.sh separately when you want to continue it.'
  fi
}

# Run the uninstaller as a child so recovery can start a new setup after it finishes.
uninstall_components() {
  local script work rc=0
  # Use the running installer's helper, never one from an older target installation.
  if [[ -n $CHECKOUT && -x $CHECKOUT/uninstall.sh ]] &&
      { [[ ${1:-} != --for-reinstall ]] || grep -Fq -- '--for-reinstall)' "$CHECKOUT/uninstall.sh"; }; then
    OWG_RAW=$OWG_RAW OWG_REF=$OWG_REF "$CHECKOUT/uninstall.sh" --dir "$DIR" "$@" "${PASSTHROUGH[@]}"
    return
  fi
  # A downloaded installer needs the uninstaller from the same source and branch.
  # Fetch it completely before removal, without changing the installed checkout.
  work=$(mktemp -d) || return 1
  script=$work/uninstall.sh
  if ! ui_step "Fetching the uninstaller ($OWG_REF)" -- curl -fsSL --retry 2 "$OWG_RAW/$OWG_REF/uninstall.sh" -o "$script"; then
    rm -rf -- "$work"
    return 1
  fi
  if ! bash -n "$script"; then
    rm -rf -- "$work"
    return 1
  fi
  OWG_RAW=$OWG_RAW OWG_REF=$OWG_REF bash "$script" --dir "$DIR" "$@" "${PASSTHROUGH[@]}" || rc=$?
  rm -rf -- "$work"
  return "$rc"
}

run_uninstaller() {
  local options=()
  ((ASSUME_YES)) && options+=(--yes)
  uninstall_components "${options[@]}"
  exit "$?"
}

reinstall() {
  find_uv
  services_from_receipt
  local options=(--root "$DIR" --embedded --non-interactive --yes --database keep --uv "$UV")
  [[ -z $SERVICES ]] || options+=(--services "$SERVICES")
  ui_section "Setting up" "Packages, settings and services"
  local rc=0
  as_owner "$DIR/scripts/install-suite.sh" "${options[@]}" || rc=$?
  ((rc == 0)) || ui_child_failed "$rc" "Reinstalling did not finish." "The lines above, from the log, say why: put that right"
  services_from_receipt
  if ! receipt_present && [[ ,$SERVICES, == *,weightroom,* ]] && ! console_account_ready; then
    if [[ ,$SERVICES, == *,loadcoach,* ]]; then TOPOLOGY=all; else TOPOLOGY=front; fi
    DATABASE=keep
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
  if ((${#UI_ATTENTION[@]})); then ui_finale "Back, with things to fix"; else ui_finale "Back in shape"; fi
  ui_attention_box
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

runtime_options() {
  RUNTIME_OPTIONS=(--strata "$STRATA_ACTION" --strata-host-ram-mib "$STRATA_HOST_RAM_MIB"
    --sdcpp "$SDCPP_ACTION" --sdcpp-backend "$SDCPP_BACKEND"
    --audiocpp "$AUDIOCPP_ACTION" --audiocpp-backend "$AUDIOCPP_BACKEND"
    --whispercpp "$WHISPERCPP_ACTION" --whispercpp-backend "$WHISPERCPP_BACKEND"
    --classifier-tools "$CLASSIFIER_TOOLS_ACTION")
}

# The service account has no sudo rights. Gather its system tools as the invoking administrator.
prepare_optional_build_tools() {
  ((SYSTEM)) || return 0
  local runtime backend label
  for runtime in STRATA SDCPP AUDIOCPP WHISPERCPP; do
    local action=${runtime}_ACTION
    [[ ${!action} == install ]] || continue
    case "$runtime" in
      STRATA) backend=cuda; label=Strata ;;
      SDCPP) backend=$SDCPP_BACKEND; label=stable-diffusion.cpp ;;
      AUDIOCPP) backend=$AUDIOCPP_BACKEND; label=audio.cpp ;;
      WHISPERCPP) backend=$WHISPERCPP_BACKEND; label=whisper.cpp ;;
    esac
    "$DIR/scripts/install-build-tools.sh" "$backend" "$label" >/dev/null ||
      ui_die "Could not prepare $label build tools for the service account." 'Fix the package or toolkit error above, then retry'
  done
}

positive_mib() { [[ $1 =~ ^[1-9][0-9]{0,8}$ ]] || { ui_warn 'Enter a positive whole number of MiB'; return 1; }; }

choose_runtimes() {
  ((ASSUME_YES)) && return 0
  local variable label default choice backend
  for variable in STRATA_ACTION SDCPP_ACTION AUDIOCPP_ACTION WHISPERCPP_ACTION CLASSIFIER_TOOLS_ACTION; do
    case "$variable" in
      STRATA_ACTION) label='Strata (CUDA text models with host RAM)' ;;
      SDCPP_ACTION) label='stable-diffusion.cpp (images)' ;;
      AUDIOCPP_ACTION) label='audio.cpp (music, sounds and speech)' ;;
      WHISPERCPP_ACTION) label='whisper.cpp (speech recognition and translation)' ;;
      CLASSIFIER_TOOLS_ACTION) label='Classifier conversion tools (CPU; matches the configured llama.cpp build)' ;;
    esac
    if [[ -z ${PLAN_GIVEN[$variable]:-} ]]; then
      default=n; [[ ${!variable} != install ]] || default=y
      if ui_confirm "Install $label? Existing installations are kept if declined." "$default"; then
        printf -v "$variable" install
      else printf -v "$variable" keep; fi
    fi
    [[ ${!variable} == install ]] || continue
    if [[ $variable == STRATA_ACTION ]]; then
      if [[ -z ${PLAN_GIVEN[STRATA_HOST_RAM_MIB]:-} ]]; then
        ui_input STRATA_HOST_RAM_MIB 'Strata may use host RAM: maximum MiB (within the worker memory limit)' "$STRATA_HOST_RAM_MIB" positive_mib
      fi
    elif [[ $variable != CLASSIFIER_TOOLS_ACTION ]]; then
      backend=${variable%_ACTION}_BACKEND
      [[ -z ${PLAN_GIVEN[$backend]:-} ]] || continue
      case "${!backend}" in cuda) default=0 ;; vulkan) default=1 ;; cpu) default=2 ;; esac
      ui_choose choice "Backend for $label" "$default" 'CUDA|NVIDIA; requires the CUDA toolkit' \
        'Vulkan|requires Vulkan build tools' 'CPU|processor only'
      local -a backends=(cuda vulkan cpu)
      printf -v "$backend" '%s' "${backends[choice]}"
    fi
  done
}

install_components() {
  # Restore the machine's role, but do not repeat engines selected during its first install.
  local COMPONENT_GIVEN="${!PLAN_GIVEN[*]}"
  local variable old_services service ticked index
  for variable in STRATA_ACTION STRATA_HOST_RAM_MIB SDCPP_ACTION SDCPP_BACKEND AUDIOCPP_ACTION AUDIOCPP_BACKEND WHISPERCPP_ACTION WHISPERCPP_BACKEND CLASSIFIER_TOOLS_ACTION; do
    PLAN_GIVEN[$variable]=1
  done
  [[ -n ${PLAN_GIVEN[LLAMA]:-} ]] || LLAMA=skip
  PLAN_GIVEN[LLAMA]=1
  resume_settings
  services_from_receipt
  old_services=$SERVICES
  # Older receipts may omit the addresses needed to render newly added services.
  if [[ -x $DIR/.venv/bin/python ]]; then
    local value recovered
    recovered=$(as_owner "$DIR/.venv/bin/python" - "$DIR" <<'PYTHON'
from pathlib import Path
import sys, tomllib
root = Path(sys.argv[1]) / 'settings'
for variable, name, key in (('FRONT_URL', 'loadcoach', 'front'), ('COMPUTE_URL', 'weightroom', 'compute'),
                            ('MACHINE_NAME', 'loadcoach', 'machine_name')):
    path = root / (name + '.toml')
    if path.is_file():
        value = tomllib.loads(path.read_text()).get(key, '')
        if isinstance(value, str) and value and not any(c in value for c in '\n\r\t'):
            print(variable + '\t' + value)
PYTHON
    ) || ui_die 'Could not read existing node addresses.' 'Fix settings/loadcoach.toml and settings/weightroom.toml before adding components'
    while IFS=$'\t' read -r variable value; do
      [[ -n $variable ]] || continue
      [[ -n ${!variable} ]] || printf -v "$variable" '%s' "$value"
    done <<<"$recovered"
  fi
  if [[ -z $TOPOLOGY ]]; then
    if [[ ,$SERVICES, == *,loadcoach,* ]]; then
      if [[ ,$SERVICES, == *,weightroom,* ]]; then TOPOLOGY=all; else TOPOLOGY=compute; fi
    else TOPOLOGY=front; fi
  fi
  local -a available=() missing=() rows=() requested=()
  case "$TOPOLOGY" in
    all) available=(loadcoach spotter weightroom turnstile toollocker voice voice-gateway) ;;
    compute) available=(loadcoach spotter voice) ;;
    front) available=(weightroom turnstile toollocker voice-gateway) ;;
  esac
  ui_section 'Install components' 'Existing services, settings and data are kept'
  for service in "${available[@]}"; do
    [[ ,$SERVICES, != *,"$service",* ]] || continue
    missing+=("$service")
    rows+=("$service|$(component_description "$service")|off")
  done
  if [[ -n $ADD_SERVICES ]]; then
    IFS=, read -ra requested <<<"$ADD_SERVICES"
  elif ((!ASSUME_YES)) && ((${#missing[@]})); then
    ui_checklist ticked 'Which additional programs should be installed?' "${rows[@]}"
    for index in $ticked; do requested+=("${missing[index]}"); done
  fi
  if [[ -n ${PLAN_GIVEN[VOICE]:-} ]]; then
    if [[ $TOPOLOGY == front ]]; then requested+=(voice-gateway); else requested+=(voice); fi
  fi
  for service in "${requested[@]}"; do
    [[ " ${available[*]} " == *" $service "* ]] || ui_die "Cannot add $service to this $TOPOLOGY installation." \
      "Choose from: ${available[*]}"
    [[ ,$SERVICES, == *,"$service",* ]] || SERVICES+=${SERVICES:+,}$service
  done
  if [[ ,$SERVICES, == *,voice,* && $TOPOLOGY == all && ,$SERVICES, != *,voice-gateway,* ]]; then SERVICES+=,voice-gateway; fi
  if [[ ,$SERVICES, == *,voice,* || ,$SERVICES, == *,voice-gateway,* ]]; then VOICE=1; fi
  if [[ $TOPOLOGY != front ]]; then
    if ((!ASSUME_YES)) && [[ $LLAMA == skip && " $COMPONENT_GIVEN " != *" LLAMA "* ]]; then
      ui_confirm 'Install llama.cpp too?' n && LLAMA=auto
    fi
    # Explicit CLI choices remain fixed; other engines are offered interactively.
    for variable in STRATA_ACTION STRATA_HOST_RAM_MIB SDCPP_ACTION SDCPP_BACKEND AUDIOCPP_ACTION AUDIOCPP_BACKEND WHISPERCPP_ACTION WHISPERCPP_BACKEND CLASSIFIER_TOOLS_ACTION; do
      [[ " $COMPONENT_GIVEN " == *" $variable "* ]] || unset 'PLAN_GIVEN[$variable]'
    done
    choose_runtimes
    if [[ $LLAMA != skip || $STRATA_ACTION == install || $SDCPP_ACTION == install || $AUDIOCPP_ACTION == install || $WHISPERCPP_ACTION == install || $CLASSIFIER_TOOLS_ACTION == install ]]; then
      [[ ,$SERVICES, == *,loadcoach,* ]] || SERVICES+=${SERVICES:+,}loadcoach
    fi
  elif [[ $STRATA_ACTION == install || $SDCPP_ACTION == install || $AUDIOCPP_ACTION == install || $WHISPERCPP_ACTION == install || $CLASSIFIER_TOOLS_ACTION == install ]]; then
    ui_die 'Model engines belong on a machine running loadcoach.' 'Run owg install on your GPU node'
  fi
  if [[ $SERVICES == "$old_services" && $LLAMA == skip && $STRATA_ACTION != install && $SDCPP_ACTION != install && $AUDIOCPP_ACTION != install && $WHISPERCPP_ACTION != install && $CLASSIFIER_TOOLS_ACTION != install ]]; then
    ui_info 'No additional components selected.'; return 0
  fi
  ui_kv Programs "$SERVICES"
  ui_kv Engines "llama.cpp: $LLAMA; Strata: $STRATA_ACTION; stable-diffusion.cpp: $SDCPP_ACTION; audio.cpp: $AUDIOCPP_ACTION; whisper.cpp: $WHISPERCPP_ACTION"
  ui_kv 'Classifier conversion tools' "$CLASSIFIER_TOOLS_ACTION"
  if ((!ASSUME_YES)); then ui_confirm 'Install these components?' y || return 0; fi
  apply_components
}

component_description() {
  case "$1" in
    loadcoach) printf 'model runner' ;; spotter) printf 'hardware monitor' ;;
    weightroom) printf 'web console' ;; turnstile) printf 'API gateway' ;;
    toollocker) printf 'tool gateway' ;; voice) printf 'speech recognition and voices' ;;
    voice-gateway) printf 'browser and phone voice' ;;
  esac
}

apply_components() {
  OPERATION=components DATABASE=keep
  write_receipt pending
  if [[ $TOPOLOGY == compute && ,$SERVICES, == *,voice,* ]] && ! as_owner test -s "$DIR/keys/voice-service.key"; then
    if [[ -z $VOICE_KEY_FILE ]]; then
      if [[ -z $JOIN_URL ]] && ((!ASSUME_YES)); then
        local host=''
        ui_input host 'Console address for a new pairing code (needed to add voice)' '' valid_console
        JOIN_URL=$(console_url "$host")
      fi
      ask_console
      [[ -z $JOIN_URL ]] || redeem
    fi
    save_machine_key
    write_receipt pending
  fi
  find_uv
  LLAMA_SERVER=''
  [[ $LLAMA == skip || $TOPOLOGY == front ]] || engine
  local options=(--root "$DIR" --embedded --non-interactive --yes --services "$SERVICES"
    --database keep --topology "$TOPOLOGY" --address "$HOST_ADDRESS" --start-new --keep-existing-units --uv "$UV")
  runtime_options
  options+=("${RUNTIME_OPTIONS[@]}")
  prepare_optional_build_tools
  [[ -z $FRONT_URL ]] || options+=(--front-url "$FRONT_URL")
  [[ -z $COMPUTE_URL ]] || options+=(--compute-url "$COMPUTE_URL")
  [[ -z $MACHINE_NAME ]] || options+=(--machine-name "$MACHINE_NAME")
  [[ -z $LLAMA_SERVER ]] || options+=(--llama-server "$LLAMA_SERVER")
  local rc=0
  as_owner "$DIR/scripts/install-suite.sh" "${options[@]}" || rc=$?
  ((rc == 0)) || ui_child_failed "$rc" 'Installing components did not finish.' 'Run the installer again to continue the saved selection'
  if [[ ,$SERVICES, == *,weightroom,* ]] && ! console_account_ready; then
    account
    finishing
  fi
  link_owg
  write_receipt complete
  receipt_load "$DIR"
  check_services
  ui_finale 'Components installed'
  ui_attention_box
}

game_plan() {
  ui_section "Game plan" "How this machine fits in"
  if [[ -z $TOPOLOGY ]]; then
    local choice=0
    ui_choose choice "How should openWeightGym run here?" 0 \
      "Everything on this machine|the console, the API and the models together: the quickest start" \
      "Console only|the web console, API and tools; GPU machines join it later" \
      "GPU node|add this machine's graphics cards to a console you already run"
    local -a topologies=(all front compute)
    TOPOLOGY=${topologies[choice]}
  fi
  if [[ $TOPOLOGY == compute ]]; then ask_console; fi
  if [[ $TOPOLOGY == front && -z $COMPUTE_URL ]] && ((!ASSUME_YES)); then
    local node=''
    ui_input node "A GPU node's address, if you have one already (Enter to add one later)" "" valid_host_or_empty
    [[ -z $node ]] || COMPUTE_URL=http://$node:8442
  fi
  if [[ $TOPOLOGY == front ]]; then
    [[ $STRATA_ACTION != install && $SDCPP_ACTION != install && $AUDIOCPP_ACTION != install && $WHISPERCPP_ACTION != install && $CLASSIFIER_TOOLS_ACTION != install ]] ||
      ui_die 'Model engines need a model runner on this machine.' 'Choose everything on this machine or install the engine on your GPU node'
  else choose_runtimes; fi
  choose_platform_services

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

choose_platform_services() {
  [[ $TOPOLOGY != compute && ! -f $DIR/settings/weightroom.toml ]] || return 0
  [[ -z ${PLAN_GIVEN[PLATFORM_SERVICES]:-} ]] || return 0
  ((ASSUME_YES)) && return 0
  ui_section 'Platform services' 'Change these later in General settings'
  local selected=() service label default
  for service in images speech audio downloads classifiers; do
    case "$service" in
      images) label='Allow image generation?' ;;
      speech) label='Allow speech (TTS, STT and Voice)?' ;;
      audio) label='Allow music and audio generation?' ;;
      downloads) label='Allow model downloads?' ;;
      classifiers) label='Allow classifier models?' ;;
    esac
    default=n
    [[ ,$PLATFORM_SERVICES, != *,$service,* ]] || default=y
    if ui_confirm "$label" "$default"; then selected+=("$service"); fi
  done
  PLATFORM_SERVICES=$(IFS=,; printf '%s' "${selected[*]}")
  PLATFORM_SERVICES=${PLATFORM_SERVICES:-none}
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
    [[ -r $VOICE_KEY_FILE ]] || ui_die "Cannot read the voice-service key in $VOICE_KEY_FILE." \
      "Copy the console's keys/voice-service.key to this machine and give its path: --voice-key-file FILE" \
      "Or join with a pairing code, which brings it: --join http://CONSOLE:8440 --code CODE"
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
    ((ASSUME_YES)) && ui_die "A GPU node needs to know its console, and --yes leaves nobody to ask." \
      "Join with a pairing code: on the console run owg pair, then here add: --join http://CONSOLE:8440 --code CODE" \
      "Or give the console's address and key: --front-url http://CONSOLE:8442 --machine-key-file FILE (a copy of its keys/machine.key)"
    ui_input host "The console machine's address (its IP or host name)" "" valid_host
    FRONT_URL=http://$host:8442
  fi
  if [[ -n $MACHINE_KEY_FILE ]]; then
    [[ -r $MACHINE_KEY_FILE ]] || ui_die "Cannot read the machine key in $MACHINE_KEY_FILE." \
      "Copy the console's keys/machine.key to this machine (scp works) and give its path: --machine-key-file FILE" \
      "Or join with a pairing code instead: --join http://CONSOLE:8440 --code CODE"
    MACHINE_KEY=$(<"$MACHINE_KEY_FILE")
  elif [[ -r $DIR/keys/machine.key ]]; then
    MACHINE_KEY=$(<"$DIR/keys/machine.key")
  else
    ((ASSUME_YES)) && ui_die "A GPU node needs the console's machine key, and --yes leaves nobody to paste it." \
      "Copy the console's keys/machine.key here and add: --machine-key-file FILE" \
      "Or join with a pairing code instead: --join http://CONSOLE:8440 --code CODE"
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
    front) SERVICES=weightroom,turnstile,toollocker; ((VOICE)) && SERVICES+=,voice-gateway ;;
    compute) SERVICES=loadcoach,spotter; ((VOICE)) && SERVICES+=,voice ;;
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
  [[ $TOPOLOGY == compute ]] || lines+=("${C_MUTED}Platform     ${C_RESET}$PLATFORM_SERVICES")
  lines+=("${C_MUTED}Data         ${C_RESET}$([[ $DATABASE == keep ]] && printf 'keep existing stores' || { [[ $DATABASE == sqlite ]] && printf 'SQLite, on this machine' || printf 'PostgreSQL, on this machine'; })")
  if [[ $TOPOLOGY != front ]]; then
    lines+=("${C_MUTED}Engine       ${C_RESET}llama.cpp · $(backend_label "$engine")")
    lines+=("${C_MUTED}Optional     ${C_RESET}Strata: $STRATA_ACTION · stable-diffusion.cpp: $SDCPP_ACTION · audio.cpp: $AUDIOCPP_ACTION; whisper.cpp: $WHISPERCPP_ACTION")
    lines+=("${C_MUTED}Conversion   ${C_RESET}Classifier tools: $CLASSIFIER_TOOLS_ACTION (CPU)")
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
    choose_runtimes
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
  valid_user_name "$ADMIN" || ui_die "$ADMIN cannot be a user name." \
    "Use lowercase letters, digits, dot, dash or underscore: --admin NAME"
  PASSWORD=${OWG_ADMIN_PASSWORD:-}
  [[ -z $PASSWORD || ${#PASSWORD} -ge 8 ]] || ui_die "The administrator password must have at least 8 characters." \
    "Set a longer one in OWG_ADMIN_PASSWORD, or leave it unset to be asked (or given a temporary one)"
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
    ui_die "uv could not fetch Python 3.13." \
      "?The lines above say why: usually the network, or github.com not answering" \
      "Check this machine can reach it: curl -sI https://github.com"
  # A GPU node is reached by the console at this address; the loopback one reaches nobody.
  if [[ $TOPOLOGY == compute && $HOST_ADDRESS == 127.0.0.1 ]]; then
    ui_attention "Could not find this machine's address on the network, so the console may not reach it" \
      "Check the network is up: ip -4 addr" \
      "Put this machine's address in $(shell_path "$DIR/settings/loadcoach.toml") (host = \"...\"), then: owg restart"
  fi
  [[ $TOPOLOGY == front ]] || engine
}

install_tools() {
  ((${#MISSING_TOOLS[@]})) || return 0
  local packages=() tool
  for tool in "${MISSING_TOOLS[@]}"; do packages+=("$(package_for "$tool")"); done
  install_packages "${packages[@]}"
}

# The installer is written with awk, sed and grep, which minimal images (openSUSE's containers)
# may lack. Nothing can be checked without them, so they are installed before anything else.
core_tools() {
  local packages=() tool
  for tool in awk sed grep; do command -v "$tool" >/dev/null || packages+=("$(package_for "$tool")"); done
  ((${#packages[@]})) || return 0
  ui_warn "Not installed: ${packages[*]}" "the installer itself needs them, so they come first"
  install_packages "${packages[@]}"
}

install_packages() {  # install_packages PACKAGE...: with sudo, never asking, or stop with what to do
  ((${#PKG_INSTALL[@]})) || ui_die "The installer needs $*, and does not know this system's package manager." \
    "Install $* with your package manager, then run the installer again"
  ui_sudo "installing $*"
  if [[ $PKG_MANAGER == apt ]]; then
    ui_step "Refreshing the package list" -- sudo -n apt-get update ||
      ui_die "apt could not refresh its package lists." "Run it yourself to see why: sudo apt-get update"
  fi
  ui_step "Installing $*" -- sudo -n "${PKG_INSTALL[@]}" "$@" ||
    ui_die "Could not install $* with $PKG_MANAGER." "?$(pkg_refresh_hint)" \
      "Then install them yourself: $(pkg_command "$@")"
}

# The account and folders of a system installation. The service account's own systemd, kept
# running by linger, starts the programs at boot; its files follow the usual places:
#   /opt/openweightgym       the code, its Python and the engine builds
#   /etc/openweightgym/keys  the secrets (opt/keys links here), and settings, a link to opt/settings
#   /var/lib/openweightgym   the account's home: data/ and models/ (opt/data and opt/models link here)
prepare_system() {
  ui_sudo "making the $SVC account and its folders"
  if ! id "$SVC" >/dev/null 2>&1; then
    # nologin is in sbin, which is not on everyone's PATH.
    local shell=/bin/false candidate
    for candidate in "$(command -v nologin || true)" /usr/sbin/nologin /sbin/nologin; do
      [[ -n $candidate && -x $candidate ]] && { shell=$candidate; break; }
    done
    ui_step "Making the $SVC account" -- sudo -n useradd --system --user-group --home-dir "$SYS_VAR" \
      --shell "$shell" --comment "openWeightGym services" "$SVC" ||
      ui_die "Could not make the $SVC account." \
        "Make it yourself: sudo useradd --system --user-group --home-dir $SYS_VAR --shell $shell $SVC"
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
    sudo -n loginctl enable-linger "$SVC" 2>>"$OWG_LOG" ||
      ui_die "Could not let the $SVC account start its services at boot." \
        "Run it yourself to see why: sudo loginctl enable-linger $SVC"
    LINGER_ENABLED=1
  fi
  # Its systemd starts a moment after linger is turned on. systemctl --user reaches it through its
  # own socket; the D-Bus one is there only where a user D-Bus session is installed.
  local uid i
  uid=$(id -u "$SVC")
  for ((i = 0; i < 20; i++)); do [[ -S /run/user/$uid/systemd/private ]] && break; sleep 0.5; done
  if [[ -S /run/user/$uid/systemd/private ]]; then ui_ok "The $SVC account starts at boot"
  else ui_warn "The $SVC account's systemd has not started yet" "if setting up cannot reach it: sudo systemctl status user@$uid.service"; fi
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
  local -a FETCH_STEPS=("?The lines above say why: usually the network, or GitHub not answering"
                        "Check this machine can reach it: git ls-remote $OWG_REPO $OWG_REF")
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
      ui_die "Could not fetch openWeightGym from $OWG_REPO." "${FETCH_STEPS[@]}"
    as_owner mv "$clone/.git" "$DIR/.git"
    as_owner rmdir "$clone"
    as_owner git -C "$DIR" reset --quiet --hard
    CREATED_DIR=1
  elif [[ -e $DIR ]] && [[ -n $(ls -A "$DIR" 2>/dev/null) ]]; then
    [[ -f $DIR/pyproject.toml && -f $DIR/scripts/install-suite.sh ]] ||
      ui_die "$DIR exists and is not openWeightGym, so nothing was put there." \
        "Choose another place: run the installer with --dir PATH" "Or empty that folder; see what is in it: ls -A $(shell_path "$DIR")"
    if ((${RESUMING_INSTALL:-0})) && [[ -z $CHECKOUT && -d $DIR/.git ]]; then
      [[ -z $(as_owner git -C "$DIR" status --porcelain --untracked-files=no) ]] ||
        ui_die "$DIR has changes of your own, and resuming would mix them with updated code." \
          "See them: git -C $(shell_path "$DIR") status" "Keep them aside: git -C $(shell_path "$DIR") stash (or commit them)"
      ui_step "Updating the interrupted installation" -- as_owner git -C "$DIR" pull --ff-only ||
        ui_die "Could not update the interrupted installation." "${FETCH_STEPS[@]}"
    fi
    ui_ok "openWeightGym is already in ${DIR/#$HOME/\~}"
    CREATED_DIR=${CREATED_DIR:-0}
  else
    ((SYSTEM)) || mkdir -p "$(dirname "$DIR")"
    ui_step "Fetching openWeightGym ($OWG_REF)" -- as_owner git clone --quiet --branch "$OWG_REF" "$OWG_REPO" "$DIR" ||
      ui_die "Could not fetch openWeightGym from $OWG_REPO." "${FETCH_STEPS[@]}"
    CREATED_DIR=1
  fi
  CHECKOUT=$DIR
  set_retry
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
    "$work/uv.tar.gz" "uv $UV_VERSION" || ui_die "Could not download uv, which fetches Python." \
    "Check this machine can reach GitHub: curl -sI https://github.com" \
    "Or install uv yourself (https://docs.astral.sh/uv/) so it is on your PATH"
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
    /*) [[ -x $LLAMA ]] || ui_die "$LLAMA is not a program, so it cannot run the models." \
          "Give the path of a llama-server that runs here: --llama /path/to/llama-server" "Or let the installer fetch one: --llama auto"
        LLAMA_SERVER=$LLAMA; ui_ok "llama.cpp: your own $LLAMA"; return 0 ;;
  esac
  local options=(--prefix "$DIR/runtimes/llamacpp") runner=()
  ((ASSUME_YES)) && options+=(--yes)
  case "$LLAMA" in
    auto) options+=(--prebuilt) ;;
    source) options+=(--from-source) ;;
    *) options+=(--prebuilt --backend "$LLAMA") ;;
  esac
  # A system installation's engine is fetched as root, which may add libraries, then handed to its account.
  # Its failure does not stop the installation, so its panel says only what went wrong with it.
  # uv can put a PATH setup script named env in ~/.local/bin; use the system program.
  local quiet=("OWG_STOP_TITLE=llama.cpp did not install" "OWG_RETRY=")
  if ((SYSTEM)); then runner=(sudo -n /usr/bin/env "OWG_LOG=$OWG_LOG" "PATH=$PATH" "TERM=${TERM:-dumb}" "${quiet[@]}")
  else runner=(/usr/bin/env "${quiet[@]}"); fi
  local executable=$DIR/runtimes/llamacpp/current/llama-server
  if ! "${runner[@]}" "$DIR/scripts/install-llamacpp.sh" "${options[@]}" || [[ ! -f $executable || ! -x $executable ]]; then
    [[ -f $executable && -x $executable ]] || ui_warn "No executable llama-server was installed at $executable"
    local again
    again="$( ((SYSTEM)) && printf 'sudo ')$(shell_path "$DIR/scripts/install-llamacpp.sh") --prefix $(shell_path "$DIR/runtimes/llamacpp")"
    ui_info "The rest of the installation goes on without it"
    ui_attention "llama.cpp is not installed, so no model can run yet" \
      "Put right what its panel above says" \
      "Then install it: $again$([[ $LLAMA == source ]] && printf ' --from-source')" \
      "Or build it on this machine, which suits any system: $again --from-source"
    return 0
  fi
  ((SYSTEM)) && sudo -n chown -R "$SVC:$SVC" "$DIR/runtimes"
  LLAMA_SERVER=$executable
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
                 --address "$HOST_ADDRESS" --start-new --uv "$UV" --database "$DATABASE" --platform-services "$PLATFORM_SERVICES")
  runtime_options
  options+=("${RUNTIME_OPTIONS[@]}")
  prepare_optional_build_tools
  [[ -n $FRONT_URL ]] && options+=(--front-url "$FRONT_URL")
  [[ -n $COMPUTE_URL ]] && options+=(--compute-url "$COMPUTE_URL")
  [[ -n ${LLAMA_SERVER:-} ]] && options+=(--llama-server "$LLAMA_SERVER")
  [[ -n $MACHINE_NAME ]] && options+=(--machine-name "$MACHINE_NAME")
  [[ $DATABASE == postgresql ]] && options+=(--install-postgresql --provision-postgresql)
  if [[ $DATABASE == postgresql ]]; then ui_sudo "installing and setting up PostgreSQL"; fi
  local rc=0
  as_owner "$DIR/scripts/install-suite.sh" "${options[@]}" || rc=$?
  ((rc == 0)) || ui_child_failed "$rc" "Setting up did not finish." "The lines above, from the log, say why: put that right"
}

finishing() {
  local weightroom=(as_owner "$DIR/.venv/bin/weightroom" --config "$DIR/settings/weightroom.toml")
  API_KEY='' TEMPORARY=''
  if [[ $TOPOLOGY != compute ]]; then
    local password_state
    password_state=$("${weightroom[@]}" user list | awk -v name="$ADMIN" 'NR > 1 && $1 == name {print $4}')
    if [[ -n $password_state ]]; then
      ui_ok "Account $ADMIN is already there"
    else
      local said
      said=$("${weightroom[@]}" user add "$ADMIN" superuser 2>>"$OWG_LOG") || ui_die "Could not add the account $ADMIN." \
        "See why near the end of the log: $OWG_LOG" \
        "Check the console's settings and data: owg doctor"
      API_KEY=$(printf '%s\n' "$said" | grep -oE 'lc_[A-Za-z0-9_-]+' | tail -1)
    fi
    # Account creation may have completed just before an interrupted password write.
    if [[ -z $password_state || $password_state == - ]]; then
      if [[ -n $PASSWORD ]]; then
        printf '%s\n' "$PASSWORD" | "${weightroom[@]}" user password "$ADMIN" --stdin >>"$OWG_LOG" 2>&1 ||
          ui_die "Could not set $ADMIN's password." \
            "See why near the end of the log: $OWG_LOG" \
            "Set it later yourself: $(shell_path "$DIR/.venv/bin/weightroom") --config $(shell_path "$DIR/settings/weightroom.toml") user password $ADMIN"
      else
        TEMPORARY=$("${weightroom[@]}" user temporary "$ADMIN" 2>>"$OWG_LOG" | tail -1)
      fi
      ui_ok "Set up the account $ADMIN"
    fi
  fi
  if ((!SYSTEM && SYSTEMD_USER && LINGER_WANTED && !LINGER)); then
    local me=${USER:-$(id -un)}
    # polkit lets most desktops do this unasked; elsewhere it needs root.
    if loginctl enable-linger "$me" 2>>"$OWG_LOG" ||
      { sudo -n true 2>/dev/null && sudo -n loginctl enable-linger "$me" 2>>"$OWG_LOG"; }; then
      ui_ok "Services keep running when you log out"
      LINGER_ENABLED=1
    else
      ui_attention "The services stop when you log out of this machine" \
        "Let them keep running: sudo loginctl enable-linger $me"
    fi
  fi
  ((POWER_RULE)) && power_rule
  link_owg
  write_receipt pending
}

# Let spotter put this machine to sleep, restart it or switch it off, through polkit.
power_rule() {
  local account=${OWG_OWNER:-${USER:-$(id -un)}} version='' javascript=0
  POLKIT_RULE=''
  if command -v pkaction >/dev/null; then version=$(pkaction --version 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)?' | head -1) || true; fi
  if [[ -z $version ]] && ! compgen -G '/usr/lib*/polkit-1/polkitd' >/dev/null && [[ ! -x /usr/libexec/polkitd ]]; then
    ui_attention "polkit is not installed, so the console cannot sleep, restart or switch off this machine" \
      "Install it: $(pkg_command "$(package_for polkit)")" \
      "Then run the installer again: $OWG_RETRY_COMMAND"
    return 0
  fi
  ui_sudo "letting the console control this machine's power"
  # polkit before 0.106 (Ubuntu 22.04) reads .pkla files, not JavaScript rules.
  if [[ -n $version ]]; then
    awk -v v="$version" 'BEGIN { exit !(v + 0 >= 0.106) }' && javascript=1
  elif [[ -d /etc/polkit-1/rules.d ]]; then
    javascript=1
  fi
  if ((javascript)); then
    POLKIT_RULE=/etc/polkit-1/rules.d/50-openweightgym-spotter.rules
    sudo -n mkdir -p /etc/polkit-1/rules.d
    sed -e "s/^var ACCOUNT = \".*\";/var ACCOUNT = \"$account\";/" -e 's/^var SERVICES = \[.*\];/var SERVICES = [];/' \
      "$DIR/services/spotter.rules" | sudo -n tee "$POLKIT_RULE" >/dev/null
  else
    POLKIT_RULE=/etc/polkit-1/localauthority/50-local.d/50-openweightgym-spotter.pkla
    sudo -n mkdir -p "${POLKIT_RULE%/*}"
    printf '%s\n' "[openWeightGym spotter: power]" "Identity=unix-user:$account" \
      "Action=org.freedesktop.login1.suspend*;org.freedesktop.login1.reboot*;org.freedesktop.login1.power-off*" \
      "ResultAny=yes" "ResultInactive=yes" "ResultActive=yes" | sudo -n tee "$POLKIT_RULE" >/dev/null
  fi
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
    if [[ $state == pending ]]; then
      echo "installer_pid=$$"
      echo "installer_start=$(awk '{print $22}' /proc/$$/stat)"
    fi
    echo "operation=${OPERATION:-install}"
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
    echo "voice=$VOICE"
    echo "platform_services=${PLATFORM_SERVICES:-images,speech,audio,downloads,classifiers}"
    echo "strata=${STRATA_ACTION:-keep}"
    echo "strata_host_ram_mib=${STRATA_HOST_RAM_MIB:-22528}"
    echo "sdcpp=${SDCPP_ACTION:-keep}"
    echo "sdcpp_backend=${SDCPP_BACKEND:-cuda}"
    echo "audiocpp=${AUDIOCPP_ACTION:-keep}"
    echo "whispercpp=${WHISPERCPP_ACTION:-keep}"
    echo "audiocpp_backend=${AUDIOCPP_BACKEND:-cuda}"
    echo "whispercpp_backend=${WHISPERCPP_BACKEND:-cuda}"
    if [[ -r $DIR/runtimes/whispercpp/current/installation.json && -x $DIR/.venv/bin/python ]]; then
      as_owner "$DIR/.venv/bin/python" -c 'import json,sys; print("whispercpp_version="+json.load(open(sys.argv[1]))["revision"])' \
        "$DIR/runtimes/whispercpp/current/installation.json"
    fi
    echo "classifier_tools=${CLASSIFIER_TOOLS_ACTION:-keep}"
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

# Each service that is not running goes on the list of things to put right at the end.
check_services() {
  local service said last title
  if ! can_start; then
    ui_info "Start the services by hand: see docs/installation.md"
    return 0
  fi
  services_from_receipt
  for service in ${SERVICES//,/ }; do
    if owg_systemctl is-active --quiet "$service.service"; then ui_ok "$service is running"
    else
      # What the program said last, in the journal: often the reason, plainly (a port taken, say).
      said=$(owg_journalctl --user-unit "$service.service" -n 40 --no-pager -o cat 2>/dev/null) || said=''
      diagnose_log "$said"
      title="$service is not running."
      if ((${#DIAGNOSIS[@]})); then title+=$'\n'"Why: ${DIAGNOSIS[0]}"
      elif last=$(grep -iE 'error|exception|failed|denied|refused|not found' <<<"$said" | tail -1) && [[ -n $last ]]; then
        title+=$'\n'"It last said: ${last:0:200}"
      fi
      ui_attention "$title" "${DIAGNOSIS_STEPS[@]}" "See all it said: owg logs $service" \
        "Put that right, then start it: owg restart $service"
    fi
  done
  return 0
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
  firewall_check "$DIR" "${SERVICES:-}" "$DIR/.venv/bin/python" as_owner
  can_start || { ui_info "systemd is not running here, so nothing was started"; return 0; }
  check_services
  local key
  # Wait only for programs that started: one that did not is already reported above.
  case "$TOPOLOGY" in
    all|front)
      if running weightroom && ! ui_step "The console answers" -- wait_http http://127.0.0.1:8440/ 60; then
        ui_attention "The console is running but does not answer on http://127.0.0.1:8440" \
          "See why: owg logs weightroom" "Put that right, then: owg restart weightroom"
      fi
      if [[ $TOPOLOGY == all ]] && running turnstile; then
        key=$(as_owner cat "$DIR/keys/weightroom.key")
        if ! ui_step "The API answers through the gateway" -- wait_http http://127.0.0.1:8442/v1/models 60 -H "Authorization: Bearer $key"; then
          ui_attention "The API gateway is running but does not answer on http://127.0.0.1:8442" \
            "See why: owg logs turnstile; owg logs loadcoach" "Put that right, then: owg restart turnstile loadcoach"
        fi
      fi
      ;;
    compute)
      if running loadcoach && ! ui_step "LoadCoach answers on $HOST_ADDRESS" -- wait_http "http://$HOST_ADDRESS:8442/v1/models" 60; then
        ui_attention "LoadCoach is running but does not answer on http://$HOST_ADDRESS:8442, so the console cannot use this node" \
          "See why: owg logs loadcoach" "Put that right, then: owg restart loadcoach"
      fi
      ;;
  esac
}

running() { owg_systemctl is-active --quiet "$1.service" 2>/dev/null; }

# ---- the finish -----------------------------------------------------------------------------

finale() {
  if ((${#UI_ATTENTION[@]})); then ui_finale "Set complete, with things to fix"; else ui_finale "Set complete"; fi
  local lines=() console_local=http://localhost:8440 console_lan=http://$HOST_ADDRESS:8440
  case "$TOPOLOGY" in
    all|front)
      lines+=("${C_MUTED}Console     ${C_RESET}${C_GOLD}$console_local${C_RESET}")
      [[ $HOST_ADDRESS != 127.0.0.1 ]] && lines+=("${C_MUTED}            ${C_RESET}$console_lan  ${C_DIM}(other devices)${C_RESET}")
      lines+=("${C_MUTED}Sign in     ${C_RESET}$ADMIN${TEMPORARY:+  ${C_DIM}temporary password:${C_RESET} $TEMPORARY}")
      lines+=("${C_MUTED}API         ${C_RESET}http://$HOST_ADDRESS:8442/v1")
      [[ -n $API_KEY ]] && lines+=("${C_MUTED}API key     ${C_RESET}$API_KEY  ${C_DIM}(shown once)${C_RESET}")
      [[ $TOPOLOGY == all ]] && lines+=("${C_MUTED}Models      ${C_RESET}download one under Models → HuggingFace, then add it from Files")
      if [[ $TOPOLOGY == front ]]; then
        lines+=("" "${C_BOLD}Add a compute node${C_RESET}: run ${C_GOLD}owg pair${C_RESET} here, or open Settings → Compute nodes,")
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
  ui_attention_box
  if ((BROWSER && !ASSUME_YES)) && [[ $TOPOLOGY != compute ]]; then ui_open_browser "$console_local" || true; fi
  printf '\n'
}

main "$@"
