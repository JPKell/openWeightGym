# shellcheck shell=bash disable=SC2034  # variables set here are for the scripts that source this
# The install receipt (what install.sh made, and where) and the account an installation runs as:
# yourself, or the openweightgym service account of a system installation. Shared by owg,
# install.sh and uninstall.sh.

[[ -n ${OWG_RECEIPT_LOADED:-} ]] && return 0
OWG_RECEIPT_LOADED=1

# Read ROOT's receipt into RECEIPT[key]. A missing receipt leaves it empty.
receipt_load() {
  declare -gA RECEIPT=()
  RECEIPT_FILE=$1/data/install-receipt
  local line
  [[ -r $RECEIPT_FILE ]] || return 0
  while IFS= read -r line; do
    [[ $line == \#* || $line != *=* ]] && continue
    RECEIPT[${line%%=*}]=${line#*=}
  done <"$RECEIPT_FILE"
  return 0
}

receipt_present() { [[ -r ${RECEIPT_FILE:-} ]]; }
receipt_get() { printf '%s' "${RECEIPT[$1]:-}"; }

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
  local owner=$1 uid home
  uid=$(id -u "$owner") home=$(getent passwd "$owner" | cut -d: -f6)
  OWNER_ENV=("HOME=$home" "USER=$owner" "XDG_RUNTIME_DIR=/run/user/$uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus"
             "PATH=/usr/local/bin:/usr/bin:/bin" "LANG=${LANG:-C.UTF-8}")
  [[ -n ${OWG_LOG:-} ]] && OWNER_ENV+=("OWG_LOG=$OWG_LOG")
  [[ -n ${UV_PYTHON_INSTALL_DIR:-} ]] && OWNER_ENV+=("UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR")
  return 0
}

# Run a command as the installation's owner. In a system installation that is the service
# account: as root directly, otherwise through sudo (asked for beforehand with ui_sudo).
as_owner() {
  local owner=${OWG_OWNER:-${RECEIPT[service_user]:-}}
  if [[ -z $owner || $owner == "$(id -un)" ]]; then "$@"; return; fi
  owner_environment "$owner"
  if ((EUID == 0)) && command -v runuser >/dev/null; then runuser -u "$owner" -- env "${OWNER_ENV[@]}" "$@"
  else sudo -n -u "$owner" env "${OWNER_ENV[@]}" "$@"; fi
}

owg_systemctl() { as_owner systemctl --user "$@"; }

# The journal of the installation's programs: owg_journalctl --user-unit X.service [options]
owg_journalctl() {
  local owner=${OWG_OWNER:-${RECEIPT[service_user]:-}}
  if [[ -z $owner || $owner == "$(id -un)" ]]; then journalctl --user "$@"; return; fi
  if ((EUID == 0)); then journalctl "_UID=$(id -u "$owner")" "$@"; else sudo -n journalctl "_UID=$(id -u "$owner")" "$@"; fi
}
