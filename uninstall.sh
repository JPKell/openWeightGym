#!/usr/bin/env bash
# Remove openWeightGym from this machine, choosing what to keep. Run it either way:
#
#   owg uninstall             or ./uninstall.sh from the installation
#   curl -fsSL https://raw.githubusercontent.com/JPKell/openWeightGym/main/uninstall.sh | bash
#
# Only what install.sh made, as its receipt lists, is removed: an installation from before
# install.sh is pointed out and left alone. Settings, keys, data and models are kept unless you
# tick them, and a backup is offered before any of them goes.
#
# Everything happens inside main(), called on the last line.

set -Eeuo pipefail

OWG_RAW=${OWG_RAW:-https://raw.githubusercontent.com/JPKell/openWeightGym}
OWG_REF=${OWG_REF:-main}

usage() {
  cat <<'EOF'
Usage: uninstall.sh [options]        (curl ... | bash -s -- [options])

  --dir PATH        the installation to remove (default: found from its receipt)
  --yes             remove the program and keep settings, keys, data and models, without asking
  --everything      remove settings, keys, data and models too (a backup is made first)
  --no-backup       with --everything: do not make the backup
  --plain           no colour or animation
  -h, --help        show this help
EOF
}

main() {
  parse_arguments "$@"
  load_ui
  ui_init
  : "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/uninstall-$(date +%Y%m%d-%H%M%S).log}"
  mkdir -p "$(dirname "$OWG_LOG")"
  export OWG_LOG
  trap 'ui_cleanup; printf "\n"; ui_fail "Stopped. What was removed so far stays removed; run this again to finish."; exit 130' INT

  printf '\n  %s%s%s  %s%s%s\n' "$C_BOLD" "$(ui_gradient openWeightGym)" "$C_RESET" "$C_MUTED" "uninstaller" "$C_RESET"
  ui_section "Cool-down" "Finding openWeightGym"
  find_installation
  inventory
  choose
  [[ ${OWG_OWNER:-} ]] && ui_sudo "removing a system installation"
  backup
  confirm
  remove
  finish
}

parse_arguments() {
  DIR=${OWG_HOME:-} ASSUME_YES=0 EVERYTHING=0 BACKUP_WANTED=''
  while (($#)); do
    case "$1" in
      --dir) DIR=${2:?--dir needs a folder}; shift 2 ;;
      --yes) ASSUME_YES=1; shift ;;
      --everything|--purge) EVERYTHING=1; shift ;;
      --no-backup) BACKUP_WANTED=0; shift ;;
      --plain) export OWG_PLAIN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "uninstall.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
  done
  ((ASSUME_YES || EVERYTHING)) && export OWG_DEFAULTS=1
  return 0
}

load_ui() {
  local self=${BASH_SOURCE[0]:-} part base
  if [[ -n $self && -f $self && -f $(dirname "$self")/scripts/lib/ui.sh ]]; then
    base=$(cd -- "$(dirname -- "$self")" && pwd)/scripts/lib
    for part in ui detect receipt; do
      # shellcheck disable=SC1090
      source "$base/$part.sh"
    done
  else
    command -v curl >/dev/null || { echo "uninstall.sh needs curl." >&2; exit 1; }
    base=$(mktemp -d)
    for part in ui detect receipt; do
      curl -fsSL --retry 2 "$OWG_RAW/$OWG_REF/scripts/lib/$part.sh" -o "$base/$part.sh" ||
        { echo "Could not fetch the uninstaller's parts from $OWG_RAW. Check the network and try again." >&2; exit 1; }
      # shellcheck disable=SC1090
      source "$base/$part.sh"
    done
    rm -rf "$base"
  fi
}

# ---- what is installed ----------------------------------------------------------------------

find_installation() {
  local -a candidates=() found=()
  local candidate owg
  [[ -n $DIR ]] && candidates+=("$DIR")
  [[ -r ${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install ]] && candidates+=("$(<"${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install")")
  [[ -r /etc/openweightgym/install ]] && candidates+=("$(</etc/openweightgym/install)")
  if owg=$(command -v owg 2>/dev/null); then candidates+=("$(dirname "$(dirname "$(readlink -f "$owg")")")"); fi
  candidates+=("$HOME/openWeightGym" /opt/openweightgym)
  for candidate in "${candidates[@]}"; do
    candidate=$(realpath -m -- "$candidate")
    [[ -r $candidate/data/install-receipt ]] || continue
    [[ " ${found[*]} " == *" $candidate "* ]] || found+=("$candidate")
    [[ -n $DIR ]] && break
  done
  if ((${#found[@]} == 0)); then
    legacy_report
    exit 0
  fi
  if ((${#found[@]} > 1)); then
    local choice=0 options=()
    for candidate in "${found[@]}"; do options+=("$candidate|$(sed -n 's/^mode=//p' "$candidate/data/install-receipt") installation"); done
    ui_choose choice "Which installation should be removed?" 0 "${options[@]}"
    DIR=${found[choice]}
  else
    DIR=${found[0]}
  fi
  receipt_load "$DIR"
  MODE=$(receipt_get mode)
  if [[ $MODE == system ]]; then OWG_OWNER=$(receipt_get service_user); export OWG_OWNER; fi
  ui_ok "Found openWeightGym in $DIR" "$(receipt_mode_label) · installed $(receipt_get installed)"
}

# An installation with no receipt was not made by install.sh: say where it is, and touch nothing.
legacy_report() {
  local unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user unit command roots=()
  for unit in loadcoach weightroom turnstile toollocker spotter voice voice-gateway frontdesk tooldoor; do
    [[ -f $unit_dir/$unit.service ]] || continue
    command=$(sed -n 's/^ExecStart=//p' "$unit_dir/$unit.service" | head -1)
    command=${command//%h/$HOME}
    [[ $command == */.venv/bin/* ]] && roots+=("${command%%/.venv/bin/*}")
  done
  if ((${#roots[@]})); then
    mapfile -t roots < <(printf '%s\n' "${roots[@]}" | sort -u)
    ui_warn "openWeightGym runs from ${roots[*]}, installed before install.sh"
    ui_note "This uninstaller removes only what install.sh made, so it leaves that installation alone."
    ui_note "To remove it by hand: stop and disable its units (systemctl --user disable --now NAME),"
    ui_note "delete the unit files in $unit_dir, then the folder, keeping a copy of settings/, keys/ and data/."
  else
    ui_info "No openWeightGym installation was found here."
  fi
}

# Folders and sizes. ITEMS is the order shown; each item has a label, a description and a default.
inventory() {
  ui_section "What is here"
  local services unit_dir count=0 service
  services=$(receipt_get services) unit_dir=$(receipt_unit_dir)
  for service in ${services//,/ }; do [[ -f $unit_dir/$service.service ]] && count=$((count + 1)); done
  ITEMS=() declare -gA LABEL=() ABOUT=() DEFAULT=()

  add_item services "Services" "stop and remove $count service$( ((count == 1)) || printf s)" on
  if [[ -n $(receipt_get owg_link) && -L $(receipt_get owg_link) ]]; then add_item command "The owg command" "$(receipt_get owg_link)" on; fi
  if [[ $(receipt_get created_dir) == 1 ]]; then
    add_item program "Program files" "the code and its Python, $(size_of_program)" on
  else
    add_item program "Python environment" "$(size_of "$DIR/.venv" "$DIR/.python" "$DIR/.tools"); your checkout itself stays" on
  fi
  [[ -e $DIR/runtimes ]] && add_item runtimes "Engine runtimes" "llama.cpp and other engines, $(size_of "$DIR/runtimes")" on
  add_item settings "Settings and keys" "settings/*.toml and keys/: this machine's setup and secrets" off
  [[ -e $DIR/data ]] && add_item data "Data" "accounts, chats, history and uploads, $(size_of "$DIR/data")" off
  [[ -e $DIR/models ]] && add_item models "Models" "downloaded model files, $(size_of "$DIR/models")" off
  if compgen -G "$DIR/keys/postgresql/*.dsn" >/dev/null 2>&1; then add_item postgresql "PostgreSQL databases" "the openWeightGym role and databases made by the installer" off; fi
  [[ $(receipt_get linger_enabled) == 1 ]] && add_item linger "Keep-running setting" "turned on by the installer (loginctl linger)" on
  [[ -n $(receipt_get polkit_rule) && -e $(receipt_get polkit_rule) ]] && add_item polkit "Power control rule" "$(receipt_get polkit_rule)" on
  [[ $MODE == system ]] && add_item account "The $(receipt_get service_user) account" "the service account system installations run as" on

  local id
  for id in "${ITEMS[@]}"; do ui_kv "${LABEL[$id]}" "${ABOUT[$id]}"; done
}

add_item() { ITEMS+=("$1"); LABEL[$1]=$2; ABOUT[$1]=$3; DEFAULT[$1]=$4; }

size_of() {
  local total
  total=$(as_reader du -scLh "$@" 2>/dev/null | tail -1 | cut -f1) || total=''
  printf '%s' "${total:-0}"
}

size_of_program() {
  local entries=() entry
  for entry in "$DIR"/* "$DIR"/.[!.]*; do
    case "${entry##*/}" in data|models|keys|settings|runtimes) continue ;; esac
    [[ -e $entry ]] && entries+=("$entry")
  done
  size_of "${entries[@]}"
}

# Read files that may belong to the service account.
as_reader() { if [[ -n ${OWG_OWNER:-} ]] && ((EUID != 0)); then sudo -n "$@" 2>/dev/null || "$@"; else "$@"; fi; }

choose() {
  local id rows=() ticked='' index
  for id in "${ITEMS[@]}"; do
    local state=${DEFAULT[$id]}
    ((EVERYTHING)) && state=on
    rows+=("${LABEL[$id]}|${ABOUT[$id]}|$state")
  done
  ui_checklist ticked "What should be removed? Unticked things are kept." "${rows[@]}"
  declare -gA REMOVE=()
  for index in $ticked; do REMOVE[${ITEMS[index]}]=1; done
  if ((${#REMOVE[@]} == 0)); then ui_info "Nothing ticked, so nothing was removed."; exit 0; fi
  # A service account that still owns kept files stays, so a reinstall finds them as they were.
  if [[ -n ${REMOVE[account]:-} ]] && { [[ -z ${REMOVE[data]:-} && -e $DIR/data ]] || [[ -z ${REMOVE[settings]:-} ]] || [[ -z ${REMOVE[models]:-} && -e $DIR/models ]]; }; then
    unset 'REMOVE[account]'
    ui_note "Keeping the $(receipt_get service_user) account: it owns the files you are keeping."
  fi
  return 0
}

removing_data() { [[ -n ${REMOVE[settings]:-}${REMOVE[data]:-}${REMOVE[models]:-}${REMOVE[postgresql]:-} ]]; }

# ---- the backup -----------------------------------------------------------------------------

backup() {
  BACKUP_FILE=''
  removing_data || return 0
  if [[ -z $BACKUP_WANTED ]]; then
    if ui_confirm "Make a backup of settings, keys and data first? (models are not included)" y; then BACKUP_WANTED=1; else BACKUP_WANTED=0; fi
  fi
  ((BACKUP_WANTED)) || return 0
  stop_services
  local stamp work parts=()
  stamp=$(date +%Y%m%d-%H%M%S)
  BACKUP_FILE=$HOME/openWeightGym-backup-$stamp.tar.gz
  work=$(mktemp -d)
  [[ -d $DIR/settings ]] && parts+=(settings)
  [[ -e $DIR/keys ]] && parts+=(keys)
  [[ -e $DIR/data ]] && parts+=(data)
  if [[ -n ${REMOVE[postgresql]:-} ]]; then dump_postgresql "$work"; fi
  local extra=()
  [[ -d $work/postgresql ]] && extra=(-C "$work" postgresql)
  ui_step "Writing the backup" -- as_reader tar --create --gzip --dereference --file "$work/backup.tar.gz" \
    --exclude='settings/defaults' --exclude='settings/examples' -C "$DIR" "${parts[@]}" "${extra[@]}" ||
    ui_die "The backup could not be written, so nothing was removed." "The log says why: $OWG_LOG"
  as_reader cat "$work/backup.tar.gz" >"$BACKUP_FILE"
  chmod 600 "$BACKUP_FILE"
  rm -rf "$work"
  ui_ok "Backup written" "$BACKUP_FILE"
}

# Each DSN file names a database; dump them all beside the files being backed up.
dump_postgresql() {
  local work=$1/postgresql dsn database
  mkdir -p "$work"
  for dsn in "$DIR"/keys/postgresql/*.dsn; do
    database=$(as_reader cat "$dsn" | sed -E 's#.*/([^/?]+).*#\1#')
    ui_step "Dumping PostgreSQL $database" -- bash -c 'sudo -n -u postgres pg_dump --format=custom "$1" >"$2"' _ "$database" "$work/$database.dump" ||
      ui_die "Could not dump $database, so nothing was removed."
  done
}

confirm() {
  local id lines=()
  for id in "${ITEMS[@]}"; do [[ -n ${REMOVE[$id]:-} ]] && lines+=("${C_ERR}remove${C_RESET}  ${LABEL[$id]}"); done
  for id in "${ITEMS[@]}"; do [[ -z ${REMOVE[$id]:-} ]] && lines+=("${C_OK}keep  ${C_RESET}  ${LABEL[$id]}"); done
  ui_box "The cool-down" "${lines[@]}"
  ((ASSUME_YES || EVERYTHING)) && return 0
  if removing_data && [[ -z $BACKUP_FILE ]]; then
    local typed=''
    ui_warn "Data will be deleted and there is no backup."
    ui_input typed "Type delete to go ahead" ""
    [[ $typed == delete ]] || { ui_info "Nothing was removed."; exit 0; }
  else
    ui_confirm "Go ahead?" n || { ui_info "Nothing was removed."; exit 0; }
  fi
}

# ---- removing -------------------------------------------------------------------------------

SERVICES_STOPPED=0
stop_services() {
  ((SERVICES_STOPPED)) && return 0
  SERVICES_STOPPED=1
  local services service stopped=()
  services=$(receipt_get services)
  for service in ${services//,/ }; do stopped=("$service" "${stopped[@]}"); done
  for service in "${stopped[@]}"; do
    owg_systemctl is-active --quiet "$service.service" 2>/dev/null || continue
    ui_step "Stopping $service" -- owg_systemctl stop "$service.service" || true
  done
}

# Delete a path, but only one that belongs to this installation. A symbolic link to a folder
# (a system installation's data and keys) takes its target with it.
remove_path() {
  local path=$1 target root allowed=0
  [[ -e $path || -L $path ]] || return 0
  target=$(realpath -m -- "$path")
  # The installation's own folders: the one it is in, and a system installation's /etc and /var parts.
  for root in "$DIR" "$(receipt_get etc_dir)" "$(receipt_get var_dir)"; do
    [[ $root == /?* && $root != "$HOME" ]] || continue
    [[ $target == "$root" || $target == "$root"/* ]] && allowed=1
  done
  if ((!allowed)) || [[ $target == / || $target == "$HOME" ]]; then
    ui_warn "Left $path alone: it is outside the installation"
    return 0
  fi
  if [[ -L $path ]]; then as_root rm -rf -- "$target"; as_root rm -f -- "$path"
  else as_root rm -rf -- "$path"; fi
}

# A system installation's files belong to its service account; removing them needs root.
as_root() { if [[ -n ${OWG_OWNER:-} ]] && ((EUID != 0)); then sudo -n "$@"; else "$@"; fi; }

remove() {
  ui_section "Cool-down" "Removing what you ticked"
  stop_services
  local service unit_dir
  if [[ -n ${REMOVE[services]:-} ]]; then
    unit_dir=$(receipt_unit_dir)
    for service in $(receipt_get services | tr , ' '); do
      owg_systemctl disable --quiet "$service.service" >>"$OWG_LOG" 2>&1 || true
      as_root rm -f -- "$unit_dir/$service.service"
    done
    owg_systemctl daemon-reload >>"$OWG_LOG" 2>&1 || true
    ui_ok "Removed the services"
  fi
  if [[ -n ${REMOVE[postgresql]:-} ]]; then drop_postgresql; fi
  [[ -n ${REMOVE[runtimes]:-} ]] && { remove_path "$DIR/runtimes"; ui_ok "Removed the engine runtimes"; }
  [[ -n ${REMOVE[models]:-} ]] && { remove_path "$DIR/models"; ui_ok "Removed the models"; }
  if [[ -n ${REMOVE[settings]:-} ]]; then
    local file
    for file in "$DIR"/settings/*.toml "$DIR"/settings/*.toml.before-*; do [[ -e $file ]] && as_root rm -f -- "$file"; done
    remove_path "$DIR/keys"
    ui_ok "Removed the settings and keys"
  fi
  [[ -n ${REMOVE[data]:-} ]] && { remove_path "$DIR/data"; ui_ok "Removed the data"; }
  if [[ -n ${REMOVE[program]:-} ]]; then remove_program; fi
  if [[ -n ${REMOVE[command]:-} ]]; then as_root rm -f -- "$(receipt_get owg_link)"; ui_ok "Removed the owg command"; fi
  if [[ -n ${REMOVE[linger]:-} ]]; then
    local who=${OWG_OWNER:-${USER:-$(id -un)}}
    as_root loginctl disable-linger "$who" 2>>"$OWG_LOG" || sudo -n loginctl disable-linger "$who" 2>>"$OWG_LOG" || true
    ui_ok "Turned off keep-running for $who"
  fi
  if [[ -n ${REMOVE[polkit]:-} ]]; then sudo -n rm -f -- "$(receipt_get polkit_rule)" && ui_ok "Removed the power control rule"; fi
  if [[ -n ${REMOVE[account]:-} ]]; then
    sudo -n loginctl disable-linger "$OWG_OWNER" 2>/dev/null || true
    sudo -n loginctl terminate-user "$OWG_OWNER" 2>/dev/null || true
    # shellcheck disable=SC2024  # the log is ours, not root's
    if sudo -n userdel "$OWG_OWNER" >>"$OWG_LOG" 2>&1; then ui_ok "Removed the $OWG_OWNER account"; else ui_warn "Could not remove the $OWG_OWNER account"; fi
    remove_path "$(receipt_get var_dir)"
  fi
  # The pointers that let owg and this uninstaller find the installation.
  if [[ ! -e $DIR/data/install-receipt ]]; then
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/openweightgym/install"
    [[ $MODE == system ]] && sudo -n rm -f /etc/openweightgym/install
    [[ $MODE == system && -d /etc/openweightgym ]] && sudo -n rmdir /etc/openweightgym 2>/dev/null || true
  fi
}

# The program's own files. A folder install.sh cloned goes, but what you are keeping in it stays;
# a checkout of your own keeps everything but the Python environment.
remove_program() {
  local entry keep
  if [[ $(receipt_get created_dir) == 1 ]]; then
    for entry in "$DIR"/* "$DIR"/.[!.]*; do
      [[ -e $entry || -L $entry ]] || continue
      keep=0
      case "${entry##*/}" in
        data) [[ -z ${REMOVE[data]:-} ]] && keep=1 ;;
        models) [[ -z ${REMOVE[models]:-} ]] && keep=1 ;;
        keys) [[ -z ${REMOVE[settings]:-} ]] && keep=1 ;;
        settings) [[ -z ${REMOVE[settings]:-} ]] && keep=1 ;;
        runtimes) [[ -z ${REMOVE[runtimes]:-} ]] && keep=1 ;;
      esac
      ((keep)) || as_root rm -rf -- "$entry"
    done
    # Only examples and defaults left in settings/ means nothing of yours is there.
    if [[ -d $DIR/settings ]] && ! compgen -G "$DIR/settings/*.toml" >/dev/null; then as_root rm -rf -- "$DIR/settings"; fi
    as_root rmdir -- "$DIR" 2>/dev/null || true
    ui_ok "Removed the program files"
  else
    remove_path "$DIR/.venv"
    remove_path "$DIR/.python"
    remove_path "$DIR/.tools"
    ui_ok "Removed the Python environment" "your checkout in $DIR is untouched"
  fi
}

drop_postgresql() {
  local dsn role database roles=()
  for dsn in "$DIR"/keys/postgresql/*.dsn; do
    [[ -e $dsn ]] || continue
    database=$(as_reader cat "$dsn" | sed -E 's#.*/([^/?]+).*#\1#')
    role=$(as_reader cat "$dsn" | sed -E 's#^postgresql://([^:@/]+).*#\1#')
    roles+=("$role")
    ui_step "Dropping PostgreSQL database $database" -- sudo -n -u postgres dropdb --if-exists "$database" || true
  done
  mapfile -t roles < <(printf '%s\n' "${roles[@]}" | sort -u)
  for role in "${roles[@]}"; do
    [[ $role == suite_* ]] || { ui_warn "Kept PostgreSQL role $role: the installer did not make it"; continue; }
    ui_step "Dropping PostgreSQL role $role" -- sudo -n -u postgres dropuser --if-exists "$role" || true
  done
}

finish() {
  ui_finale "Cool-down complete"
  local lines=() id kept=0
  for id in "${ITEMS[@]}"; do
    if [[ -z ${REMOVE[$id]:-} ]]; then lines+=("${C_OK}kept${C_RESET}     ${LABEL[$id]}"); kept=1; fi
  done
  [[ -n $BACKUP_FILE ]] && lines+=("" "${C_MUTED}Backup   ${C_RESET}$BACKUP_FILE")
  if ((kept)) && [[ -e $DIR ]]; then
    lines+=("" "What you kept is in ${DIR/#$HOME/\~}. Installing again there picks it up.")
  fi
  lines+=("" "${C_DIM}Log: ${OWG_LOG/#$HOME/\~}${C_RESET}")
  ((${#lines[@]} > 2)) || lines=("Everything is gone. Thanks for training with openWeightGym." "${lines[@]}")
  ui_box "Done" "${lines[@]}"
  printf '\n'
}

main "$@"
