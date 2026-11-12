# shellcheck shell=bash disable=SC2034  # variables set here are for the scripts that source this
# The install receipt (what install.sh made, and where) and the account an installation runs as:
# yourself, or the openweightgym service account of a system installation. Shared by owg,
# install.sh and uninstall.sh.

[[ -n ${OWG_RECEIPT_LOADED:-} ]] && return 0
OWG_RECEIPT_LOADED=1
declare -gA RECEIPT=()
RECEIPT_LOADED=0

# Find public metadata first; older installations kept the receipt inside private data/.
receipt_find() {
  local root=$1 pointer
  if [[ -e $root/.install-receipt || -L $root/.install-receipt ]]; then
    printf '%s' "$root/.install-receipt"
  elif [[ -r $root/data/install-receipt ]]; then
    printf '%s' "$root/data/install-receipt"
  else
    pointer=${OWG_SYSTEM_ETC:-/etc/openweightgym}/install
    if [[ -r $pointer && $(realpath -m -- "$(<"$pointer")") == "$(realpath -m -- "$root")" ]]; then
      printf '%s' "$root/data/install-receipt"
      return 0
    fi
    return 1
  fi
}

# Read ROOT's metadata literally, never as shell code. Legacy system receipts need sudo once.
receipt_load() {
  declare -gA RECEIPT=()
  RECEIPT_LOADED=0
  RECEIPT_FILE=$(receipt_find "$1") || { RECEIPT_FILE=$1/.install-receipt; return 0; }
  local line contents
  if [[ -r $RECEIPT_FILE ]]; then
    contents=$(cat -- "$RECEIPT_FILE") || return 1
  else
    ui_sudo "reading this system installation's legacy receipt"
    contents=$(sudo -n cat -- "$RECEIPT_FILE") || ui_die "Cannot read the installation receipt in $1, even with sudo." \
      "Check sudo works here: sudo -v" "Check the file is there: sudo ls -l $RECEIPT_FILE"
  fi
  while IFS= read -r line; do
    [[ $line == \#* || $line != *=* ]] && continue
    RECEIPT[${line%%=*}]=${line#*=}
  done <<<"$contents"
  RECEIPT_LOADED=1
  return 0
}

receipt_present() { (( ${RECEIPT_LOADED:-0} )); }
receipt_get() { printf '%s' "${RECEIPT[$1]:-}"; }

# Publish only nonsecret fields when upgrading an old receipt. Keep the old copy for rollback.
receipt_publish() {
  local root=$1 key
  [[ ${RECEIPT_FILE:-} != "$root/.install-receipt" ]] || return 0
  receipt_present || return 0
  {
    for key in version state installed mode dir created_dir topology services unit_dir python venv uv runtime models \
      linger_enabled owg_link pointer database admin polkit_rule service_user etc_dir var_dir \
      llama voice linger_wanted power_rule front_url compute_url machine_name join_url operation \
      strata strata_host_ram_mib sdcpp sdcpp_backend audiocpp audiocpp_backend installer_pid installer_start; do
      [[ -v RECEIPT[$key] ]] && printf '%s=%s\n' "$key" "${RECEIPT[$key]}"
    done
    :
  } | as_owner bash -c 'umask 022; cat >"$1.new" && chmod 644 "$1.new" && mv "$1.new" "$1"' _ "$root/.install-receipt"
  RECEIPT_FILE=$root/.install-receipt
}

receipt_unit_dir() {
  local dir=${RECEIPT[unit_dir]:-}
  printf '%s' "${dir:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
}

receipt_mode_label() {
  if [[ ${RECEIPT[mode]:-user} == system ]]; then printf 'System installation, runs as %s' "${RECEIPT[service_user]:-openweightgym}"
  else printf 'Installed for %s' "${USER:-$(id -un)}"; fi
}

# The environment a service account's own systemd needs: OWNER_ENV=(HOME=... XDG_RUNTIME_DIR=...).
owner_environment() {
  local owner=$1 owner_uid owner_home
  owner_uid=$(id -u "$owner") owner_home=$(getent passwd "$owner" | cut -d: -f6)
  OWNER_ENV=("HOME=$owner_home" "USER=$owner" "XDG_RUNTIME_DIR=/run/user/$owner_uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$owner_uid/bus"
             "XDG_CONFIG_HOME=$owner_home/.config" "XDG_STATE_HOME=$owner_home/.local/state" "XDG_CACHE_HOME=$owner_home/.cache"
             "PATH=/usr/local/bin:/usr/bin:/bin" "LANG=${LANG:-C.UTF-8}")
  # The account's own log: it cannot write to the one in your home folder.
  [[ -n ${OWG_OWNER_LOG:-} ]] && OWNER_ENV+=("OWG_LOG=$OWG_OWNER_LOG")
  [[ -n ${UV_PYTHON_INSTALL_DIR:-} ]] && OWNER_ENV+=("UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR")
  # A custom toolkit selected by the administrator must also be visible during owner-run builds.
  local variable
  for variable in CUDACXX CUDAToolkit_ROOT CUDA_PATH CUDA_HOME ROCM_PATH HIP_PATH; do
    [[ -z ${!variable:-} ]] || OWNER_ENV+=("$variable=${!variable}")
  done
  return 0
}

# Run a command as the installation's owner. In a system installation that is the service
# account: as root directly, otherwise through sudo (asked for beforehand with ui_sudo).
as_owner() {
  local owner=${OWG_OWNER:-${RECEIPT[service_user]:-}}
  if [[ -z $owner || $owner == "$(id -un)" ]]; then "$@"; return; fi
  owner_environment "$owner"
  # From / : tools such as uv look for their settings in the current folder and its parents, and
  # stop with an error at a home folder (yours, mode 700 on Fedora and Arch) the account cannot enter.
  if ((EUID == 0)) && command -v runuser >/dev/null; then (cd / && runuser -u "$owner" -- env "${OWNER_ENV[@]}" "$@")
  else (cd / && sudo -n -u "$owner" env "${OWNER_ENV[@]}" "$@"); fi
}

owg_systemctl() { as_owner systemctl --user "$@"; }

# Existing runtime pins must never outrank a successful code update. Keep custom libraries
# and other unit settings; the Python helper backs up only files whose source order changes.
refresh_runtime_sources() {
  local root=$1 program
  shift
  [[ -x $root/.venv/bin/python && -f $root/scripts/update-runtime-source.py ]] || return 0
  local -a options=(--root "$root" --unit-dir "$(receipt_unit_dir)" --apply)
  for program in "$@"; do options+=(--service "$program"); done
  ui_step "Selecting current checkout for services" -- as_owner "$root/.venv/bin/python" \
    "$root/scripts/update-runtime-source.py" "${options[@]}"
}

# The journal of the installation's programs: owg_journalctl --user-unit X.service [options]
owg_journalctl() {
  local owner=${OWG_OWNER:-${RECEIPT[service_user]:-}}
  if [[ -z $owner || $owner == "$(id -un)" ]]; then journalctl --user "$@"; return; fi
  if ((EUID == 0)); then journalctl "_UID=$(id -u "$owner")" "$@"; else sudo -n journalctl "_UID=$(id -u "$owner")" "$@"; fi
}
