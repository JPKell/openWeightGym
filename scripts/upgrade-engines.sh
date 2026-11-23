#!/usr/bin/env bash
# owg upgrade: bring the installed model engines up to date.
#
#   owg upgrade [--check] [--reinstall] [--llama-build TAG] [ENGINE...]
#
# ENGINE is llama.cpp, sdcpp (stable-diffusion.cpp), audio.cpp, whisper.cpp or strata; every
# installed engine by default. llama.cpp moves to the newest ggml-org release (or TAG), keeping
# its backend and its prebuilt or source method. sdcpp (stable-diffusion.cpp) moves to the newest
# upstream commit the same way. The other engines build the revision this checkout qualifies: a newer one, after owg update, is installed beside the current release;
# otherwise there is no upgrade to take, so the release is set aside, rebuilt cleanly, and the
# old one deleted once the new one is verified (or put back if the rebuild fails).
# --check shows the plan and changes nothing; --reinstall also rebuilds an up-to-date llama.cpp.
# Nothing is restarted: new model launches use the new builds; owg restart loadcoach applies
# them to models already loaded.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib/ui.sh
source "$SCRIPT_DIR/lib/ui.sh"
# shellcheck source=lib/detect.sh
source "$SCRIPT_DIR/lib/detect.sh"
# shellcheck source=lib/receipt.sh
source "$SCRIPT_DIR/lib/receipt.sh"

usage() { sed -n '2,/^set /{/^#/!q;s/^# \{0,1\}//p}' "${BASH_SOURCE[0]}"; }
usage_error() { OWG_STOP_TITLE='Nothing was changed' OWG_RETRY='' ui_die --status 2 "$1" 'See every option: owg upgrade --help'; }

CHECK=0 REINSTALL=0 LLAMA_BUILD=''
CHOSEN=()

parse_arguments() {
  while (($#)); do
    case "$1" in
      --dir) [[ -n ${2:-} ]] || usage_error '--dir needs a folder.'; ROOT=$(cd -- "$2" && pwd); shift 2 ;;
      --check) CHECK=1; shift ;;
      --reinstall) REINSTALL=1; shift ;;
      --llama-build) [[ -n ${2:-} ]] || usage_error '--llama-build needs a release, such as b12359.'
                     LLAMA_BUILD=$2; shift 2 ;;
      --llama-build=*) LLAMA_BUILD=${1#*=}; shift ;;
      --yes) export OWG_DEFAULTS=1; shift ;;
      --plain) export OWG_PLAIN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      -*) usage_error "owg upgrade has no option $1." ;;
      *) CHOSEN+=("$1"); shift ;;
    esac
  done
  [[ -z $LLAMA_BUILD || $LLAMA_BUILD =~ ^(b[0-9]+|v[0-9]+\.[0-9]+\.[0-9]+)$ ]] ||
    usage_error "A llama.cpp release looks like b12359 or v1.0.0, not $LLAMA_BUILD."
}

# A commit is shown by its first 12 characters; release tags are shown whole.
short() { if [[ $1 =~ ^[0-9a-f]{40}$ ]]; then printf '%s' "${1:0:12}"; else printf '%s' "$1"; fi; }

helper() { as_owner "$ROOT/.venv/bin/python" "$ROOT/scripts/engine-upgrades.py" "$@" --root "$ROOT"; }

SYSTEM=0 SERVICE_USER=''
# llama.cpp is fetched as root in a system installation, which may add libraries, then handed to its account.
as_installer() {
  if ((SYSTEM)); then sudo -n /usr/bin/env "OWG_LOG=$OWG_LOG" "PATH=$PATH" "TERM=${TERM:-dumb}" "$@"
  else /usr/bin/env "$@"; fi
}

# Install build tools in the open, where sudo can ask, before a build runs behind a spinner.
build_tools() {
  local backend=$1 label=$2
  "$ROOT/scripts/install-build-tools.sh" "$backend" "$label" >/dev/null
}

# install ENGINE LABEL BACKEND FOLDER EXTRA TARGET: the engine's own installer, into the same prefix.
install_engine() {
  local engine=$1 label=$2 backend=$3 folder=$4 extra=$5 target=$6 prefix
  prefix=$(dirname -- "$(dirname -- "$folder")")
  case "$engine" in
    llamacpp)
      local method=--prebuilt
      [[ $extra == source ]] && method=--from-source
      as_installer OWG_STOP_TITLE="llama.cpp did not install" OWG_RETRY= \
        "$ROOT/scripts/install-llamacpp.sh" --prefix "$prefix" --build "$target" --backend "$backend" "$method" --yes || return
      if ((SYSTEM)); then sudo -n chown -R "$SERVICE_USER:$SERVICE_USER" "$prefix"; fi
      return 0 ;;
    sdcpp)
      build_tools "$backend" "$label" || return
      local options=(--root "$ROOT" --prefix "$prefix" --backend "$backend" --revision "$target")
      ui_step "Building $label ($backend)" -- as_owner "$ROOT/.venv/bin/python" "$ROOT/scripts/install-sdcpp.py" "${options[@]}" ;;
    audiocpp)
      build_tools "$backend" "$label" || return
      ui_step "Building $label ($backend)" -- as_owner "$ROOT/.venv/bin/python" "$ROOT/soundroom/install.py" \
        --root "$ROOT" --prefix "$prefix" --backend "$backend" ;;
    whispercpp)
      build_tools "$backend" "$label" || return
      ui_step "Building $label ($backend)" -- as_owner "$ROOT/.venv/bin/python" "$ROOT/engines/whisper_cpp/install.py" \
        --root "$ROOT" --prefix "$prefix" --backend "$backend" ;;
    strata)
      build_tools cuda "$label" || return
      ui_step "Building $label (cuda)" -- as_owner "$ROOT/.venv/bin/python" "$ROOT/engines/strata/install.py" \
        --root "$ROOT" --prefix "$prefix" --unit-dir "$(receipt_unit_dir)" --host-ram-mib "$extra" ;;
  esac
}

# Rebuild the release in use from nothing: set it aside, install, then delete or restore it.
reinstall_engine() {
  local engine=$1 label=$2 aside
  aside=$(helper set-aside "$engine") || { ui_fail "$label: could not set the current release aside" 2>&1; return 1; }
  ui_info "$label: previous release set aside" "${aside/#$HOME/\~}"
  if install_engine "$@"; then
    helper discard "$engine" >/dev/null || ui_warn "$label: could not delete the previous release" "${aside/#$HOME/\~}"
    return 0
  fi
  if helper restore "$engine" >/dev/null; then ui_info "$label: the previous release is back in use"
  else ui_fail "$label: could not put the previous release back" "it is in ${aside/#$HOME/\~}" 2>&1; fi
  return 1
}

main() {
  parse_arguments "$@"
  ui_init
  : "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/upgrade-$(date +%Y%m%d-%H%M%S).log}"
  ui_log_init 2>/dev/null || OWG_LOG=/dev/null
  export OWG_LOG
  receipt_load "$ROOT"
  [[ $(receipt_get mode) == system ]] && SYSTEM=1
  SERVICE_USER=$(receipt_get service_user)
  [[ -x $ROOT/.venv/bin/python ]] || ui_die "$ROOT has no Python environment." 'Repair it first: owg repair'
  [[ -f $ROOT/settings/loadcoach.toml ]] || ui_die 'Model engines run beside loadcoach, and this machine has none.' \
    'Run owg upgrade on your GPU node'
  if [[ -n $SERVICE_USER && $SERVICE_USER != "$(id -un)" ]] && ((EUID != 0)); then
    ui_sudo "upgrading engines for the $SERVICE_USER service account"
  fi

  local -A wanted=()
  local name
  for name in "${CHOSEN[@]}"; do
    name=$(helper name "$name") || usage_error "$name is not an engine: choose llama.cpp, sdcpp, audio.cpp, whisper.cpp or strata."
    wanted[$name]=1
  done

  ui_section 'Upgrade engines' "${ROOT/#$HOME/\~}"
  local plan options=()
  [[ -z $LLAMA_BUILD ]] || options+=(--llama-build "$LLAMA_BUILD")
  ui_info 'Looking for newer releases'
  plan=$(helper plan "${options[@]}") || ui_die 'Could not read the installed engines.' 'Check settings/loadcoach.toml, then retry'

  local -a rows=() labels=()
  local engine label action installed target backend folder extra note mark
  while IFS=$'\x1f' read -r engine label action installed target backend folder extra note; do
    [[ -n $engine ]] || continue
    if ((${#CHOSEN[@]})) && [[ -z ${wanted[$engine]:-} ]]; then continue; fi
    wanted[$engine]=found
    [[ $action == current && $REINSTALL == 1 ]] && action=reinstall note="rebuilding $installed as asked"
    case "$action" in
      upgrade) ui_ok "$label" "$installed → $(short "$target")  ($backend)"; mark=on ;;
      reinstall) ui_warn "$label" "$installed: reinstall  ($backend)"; mark=on ;;
      current) ui_ok "$label" "$installed is current"; mark='' ;;
      unchecked) ui_warn "$label" "$installed: could not check for a newer release"; mark='' ;;
      unavailable) ui_warn "$label" "$installed: $target has no matching download"; mark='' ;;
      *) ui_warn "$label" "not managed here"; mark='' ;;
    esac
    [[ -z $note ]] || ui_note "$note"
    [[ -n $mark ]] || continue
    local field=$'\x1f'
    rows+=("$engine$field$label$field$action$field$backend$field$folder$field$extra$field$target$field$installed")
    labels+=("$label|$action $([[ $action == upgrade ]] && printf '%s → %s' "$installed" "$(short "$target")" || printf '%s' "$installed")|on")
  done <<<"$plan"
  for name in "${!wanted[@]}"; do
    [[ ${wanted[$name]} == found ]] || ui_warn "$name is not installed here" 'add it with owg install'
  done
  ((${#rows[@]})) || { ui_info 'Nothing to upgrade.'; return 0; }
  ((CHECK)) && { ui_info 'Nothing was changed (--check).'; return 0; }

  local ticked index
  ui_checklist ticked 'Which engines should be upgraded or reinstalled?' "${labels[@]}"
  [[ -n $ticked ]] || { ui_info 'Nothing was changed.'; return 0; }
  ui_note 'Builds from source take a while. A reinstalled engine cannot start new models until its rebuild finishes.'
  ui_note "The log is $OWG_LOG"

  local failed=() done_=()
  for index in $ticked; do
    IFS=$'\x1f' read -r engine label action backend folder extra target installed <<<"${rows[index]}"
    ui_section "$label" "$([[ $action == upgrade ]] && printf 'Upgrading %s → %s' "$installed" "$(short "$target")" || printf 'Reinstalling %s' "$installed")"
    # A reinstall rebuilds the same llama.cpp release; the planned target is the newest one otherwise.
    [[ $action == reinstall && $engine == llamacpp ]] && target=$installed
    if [[ $action == upgrade ]]; then
      if install_engine "$engine" "$label" "$backend" "$folder" "$extra" "$target"; then done_+=("$label"); else failed+=("$label"); fi
    else
      if reinstall_engine "$engine" "$label" "$backend" "$folder" "$extra" "$target"; then done_+=("$label"); else failed+=("$label"); fi
    fi
  done

  refresh_component_versions "$ROOT" || ui_warn 'Component versions were not recorded' 'owg restart records them again'
  printf '\n'
  ((${#done_[@]} == 0)) || ui_ok "Done: ${done_[*]}" 'new model launches use the new builds'
  if ((${#done_[@]})); then
    ui_info "Models already loaded keep the old build until LoadCoach restarts: owg restart loadcoach"
  fi
  if ((${#failed[@]})); then
    ui_fail "Did not finish: ${failed[*]}" "the lines above and $OWG_LOG say why" 2>&1
    return 1
  fi
}

main "$@"
