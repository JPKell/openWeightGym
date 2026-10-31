# shellcheck shell=bash disable=SC2034  # variables set here are for the scripts that source this
# openWeightGym's look in a terminal, shared by install.sh, scripts/owg and the uninstaller: the
# barbell banner, colour, spinners, progress bars, menus and the finish. Pure bash 4 or newer.
#
# Colour and animation switch themselves off when output is not a terminal, NO_COLOR is set,
# TERM is dumb or OWG_PLAIN=1; OWG_QUIET=1 keeps colour and skips the animations. OWG_COLOR=24,
# 256, 16 or 0 forces a colour depth. Questions are read from /dev/tty, so `curl ... | bash`
# can still ask them; with no terminal at all, or OWG_DEFAULTS=1, every question takes its default.
#
# Safe under `set -euo pipefail`. Long-running commands go through ui_step or ui_download, which
# send their output to $OWG_LOG and show it only when something fails.

[[ -n ${OWG_UI_LOADED:-} ]] && return 0
OWG_UI_LOADED=1
shopt -s extglob

# The console's palettes, left to right: Sunflower, Clay, Rose, Orchid, Indigo, Carbon Cyan.
UI_STOPS=(f4d75e eeb08d f0a4c2 c8a6ed b4b3ff 6dd9e8)
OWG_TAGLINE=${OWG_TAGLINE:-"local models · heavy lifting"}

ui_init() {
  UI_OUT_TTY=0; [[ -t 1 ]] && UI_OUT_TTY=1
  UI_IN_TTY=0
  if [[ ${OWG_NO_TTY:-0} != 1 ]] && { exec {UI_TTY_FD}<>/dev/tty; } 2>/dev/null; then UI_IN_TTY=1; fi
  UI_ASK=$UI_IN_TTY  # questions are asked; otherwise each takes its default
  [[ ${OWG_DEFAULTS:-0} == 1 ]] && UI_ASK=0

  # Block characters only when bash itself counts them as one character each.
  UI_UTF8=0
  local probe='█'
  if ((${#probe} == 1)); then
    UI_UTF8=1
  elif [[ ${LC_ALL:-${LC_CTYPE:-${LANG:-}}} == *[Uu][Tt][Ff]?(-)8* ]]; then
    LC_ALL=C.UTF-8 2>/dev/null
    probe='█'; ((${#probe} == 1)) && UI_UTF8=1
  fi

  UI_COLOR=0
  if ((UI_OUT_TTY)) && [[ -z ${NO_COLOR:-} && ${TERM:-dumb} != dumb && ${OWG_PLAIN:-0} != 1 ]]; then
    case "${COLORTERM:-}:${TERM:-}" in
      truecolor:*|24bit:*|*:xterm-kitty|*:alacritty|*:foot*|*:wezterm|*:xterm-ghostty) UI_COLOR=24 ;;
      *:*256color*) UI_COLOR=256 ;;
      *) UI_COLOR=16 ;;
    esac
  fi
  [[ ${OWG_COLOR:-} =~ ^(24|256|16|0)$ ]] && UI_COLOR=$OWG_COLOR
  UI_ANIM=0
  if ((UI_COLOR && UI_IN_TTY)) && [[ ${OWG_QUIET:-0} != 1 && -z ${CI:-} ]]; then UI_ANIM=1; fi

  UI_COLS=${COLUMNS:-}
  if [[ -z $UI_COLS ]] && ((UI_IN_TTY)); then
    UI_COLS=$(stty size <&"$UI_TTY_FD" 2>/dev/null | cut -d' ' -f2) || true
  fi
  [[ $UI_COLS =~ ^[0-9]+$ ]] && ((UI_COLS >= 20)) || UI_COLS=80

  if ((UI_COLOR)); then
    C_RESET=$'\e[0m' C_BOLD=$'\e[1m' C_DIM=$'\e[2m' C_ITALIC=$'\e[3m'
    ui__rgb f1ca69; C_GOLD=$REPLY
    ui__rgb a4e878; C_OK=$REPLY
    ui__rgb f1ca69; C_WARN=$REPLY
    ui__rgb ff6b6b; C_ERR=$REPLY
    ui__rgb 6dd9e8; C_CYAN=$REPLY
    ui__rgb c8a6ed; C_ORCHID=$REPLY
    if ((UI_COLOR == 16)); then C_MUTED=$'\e[37m'; else ui__rgb babac7; C_MUTED=$REPLY; fi
  else
    C_RESET='' C_BOLD='' C_DIM='' C_ITALIC='' C_GOLD='' C_OK='' C_WARN='' C_ERR='' C_CYAN='' C_ORCHID='' C_MUTED=''
  fi
  C_EL=''; ((UI_OUT_TTY)) && C_EL=$'\e[K'  # clear to the end of the line

  if ((UI_UTF8)); then
    S_OK='✔' S_FAIL='✘' S_WARN='▲' S_INFO='›' S_DOT='·' S_POINT='❯' S_ON='◉' S_OFF='○' S_DIAMOND='◆' S_ELLIPSIS='…'
    UI_LIFT=('⣤⣀⣀⣀⣤' '⣤⣀⣀⣀⣤' '⠶⠤⠤⠤⠶' '⠛⠒⠒⠒⠛' '⠛⠒⠒⠒⠛' '⠛⠒⠒⠒⠛' '⠶⠤⠤⠤⠶' '⣤⣀⣀⣀⣤')
  else
    S_OK='ok' S_FAIL='!!' S_WARN='!' S_INFO='>' S_DOT='-' S_POINT='>' S_ON='[x]' S_OFF='[ ]' S_DIAMOND='*' S_ELLIPSIS='...'
    UI_LIFT=('o___o' 'o---o' 'o^^^o' 'o---o')
  fi

  # A sleep without a process each time: read from a pipe nothing is ever written to.
  UI_SLEEP_FD=
  { exec {UI_SLEEP_FD}<> <(:); } 2>/dev/null || UI_SLEEP_FD=
  trap ui_cleanup EXIT
}

# Put the terminal back as it was: cursor shown, echo on, and no step left running behind a spinner.
ui_cleanup() {
  if [[ -n ${UI_BG_PID:-} ]] && kill -0 "$UI_BG_PID" 2>/dev/null; then kill "$UI_BG_PID" 2>/dev/null || true; fi
  if ((${UI_OUT_TTY:-0})); then printf '\e[?25h' 2>/dev/null || true; fi
  if ((${UI_IN_TTY:-0})); then stty echo <&"$UI_TTY_FD" 2>/dev/null || true; fi
}

ui__sleep() {
  if [[ -n ${UI_SLEEP_FD:-} ]]; then read -r -t "$1" -u "$UI_SLEEP_FD" _ 2>/dev/null || true; else sleep "$1"; fi
}

# Wait $1 seconds, or less if a key is pressed: returns 0 when one was.
ui__pause_or_key() {
  ((UI_IN_TTY)) || { ui__sleep "$1"; return 1; }
  local key
  IFS= read -r -s -n1 -t "$1" key <&"$UI_TTY_FD" 2>/dev/null
}

# REPLY = the foreground escape for a hex colour, at this terminal's depth.
ui__rgb() {
  local hex=$1 r g b
  r=$((16#${hex:0:2})) g=$((16#${hex:2:2})) b=$((16#${hex:4:2}))
  ui__rgb3 "$r" "$g" "$b"
}

ui__rgb3() {
  local r=$1 g=$2 b=$3
  case "${UI_COLOR:-0}" in
    24) REPLY=$'\e[38;2;'"$r;$g;${b}m" ;;
    256) REPLY=$'\e[38;5;'"$((16 + 36 * ((r * 5 + 127) / 255) + 6 * ((g * 5 + 127) / 255) + (b * 5 + 127) / 255))m" ;;
    16)
      local top=$r code=90
      ((g > top)) && top=$g
      ((b > top)) && top=$b
      ((r * 4 >= top * 3)) && code=$((code + 1))
      ((g * 4 >= top * 3)) && code=$((code + 2))
      ((b * 4 >= top * 3)) && code=$((code + 4))
      REPLY=$'\e['"${code}m" ;;
    *) REPLY='' ;;
  esac
}

# REPLY = the gradient's colour at position $1 of $2.
ui__gradient_at() {
  local pos=$1 total=$2 segments=$((${#UI_STOPS[@]} - 1)) t seg f a b
  ((total > 1)) || total=2
  t=$((pos * segments * 1000 / (total - 1)))
  seg=$((t / 1000)) f=$((t % 1000))
  ((seg >= segments)) && seg=$((segments - 1)) f=1000
  a=${UI_STOPS[seg]} b=${UI_STOPS[seg + 1]}
  ui__rgb3 $(((16#${a:0:2} * (1000 - f) + 16#${b:0:2} * f) / 1000)) \
           $(((16#${a:2:2} * (1000 - f) + 16#${b:2:2} * f) / 1000)) \
           $(((16#${a:4:2} * (1000 - f) + 16#${b:4:2} * f) / 1000))
}

# Print $1 coloured along the gradient, spread over $2 columns (default: its own length).
ui_gradient() {
  local text=$1 width=${2:-${#1}} out='' i ch
  if ((!UI_COLOR)); then printf '%s' "$text"; return; fi
  for ((i = 0; i < ${#text}; i++)); do
    ch=${text:i:1}
    if [[ $ch == ' ' ]]; then out+=' '; else ui__gradient_at "$i" "$width"; out+="$REPLY$ch"; fi
  done
  printf '%s%s' "$out" "$C_RESET"
}

# The visible length of a string that may hold colour escapes.
ui__width() {
  local plain=${1//$'\e'\[*([0-9;])m/}
  REPLY=${#plain}
}

ui__repeat() {  # REPLY = $1 repeated $2 times
  local out='' i
  for ((i = 0; i < $2; i++)); do out+=$1; done
  REPLY=$out
}

# ---- the banner ---------------------------------------------------------------------------

ui__barbell_rows() {
  UI_ROWS=()
  local r sleeve gap bar
  local -a smalls bigs
  if ((UI_UTF8)); then
    smalls=('   ' '▗█▖' '▐█▌' '▝█▘' '   ')
    bigs=('▗███▖' '▐███▌' '▐███▌' '▐███▌' '▝███▘')
    ui__repeat '━' 28; bar=$REPLY
  else
    smalls=('   ' '.-.' '|#|' "'-'" '   ')
    bigs=('.---.' '|###|' '|###|' '|###|' "'---'")
    ui__repeat '=' 28; bar=$REPLY
  fi
  for r in 0 1 2 3 4; do
    if ((r == 2)); then
      sleeve=${bar:0:2} gap=${bar:0:1}
      UI_ROWS+=("$sleeve${smalls[r]}$gap${bigs[r]}$bar${bigs[r]}$gap${smalls[r]}$sleeve")
    else
      UI_ROWS+=("  ${smalls[r]} ${bigs[r]}                            ${bigs[r]} ${smalls[r]}  ")
    fi
  done
}

# The barbell, the name and the tagline. Animated once, unless a key is pressed or animation is off.
ui_banner() {
  local version=${1:-} indent width=50 r c frame skip=0 lines word tag pad
  ui__barbell_rows
  word="o p e n W e i g h t G y m"
  tag="$OWG_TAGLINE${version:+ $S_DOT v$version}"
  if ((UI_COLS < width + 4)); then
    printf '\n  %s%s%s\n  %s%s%s\n\n' "$C_BOLD" "$(ui_gradient openWeightGym)" "$C_RESET" "$C_MUTED" "$tag" "$C_RESET"
    return 0
  fi
  indent=$(((UI_COLS - width) / 2)); ((indent > 6)) && indent=6
  printf -v pad '%*s' "$indent" ''

  # Each column's colour, worked out once.
  local -a cells=()
  for ((r = 0; r < 5; r++)); do
    for ((c = 0; c < width; c++)); do
      local ch=${UI_ROWS[r]:c:1}
      if [[ $ch == ' ' || $UI_COLOR == 0 ]]; then cells[r * width + c]=$ch; else ui__gradient_at "$c" "$width"; cells[r * width + c]=$REPLY$ch; fi
    done
  done
  ui__row() {  # print row $1 up to column $2
    local out='' k
    for ((k = 0; k < $2; k++)); do out+=${cells[$1 * width + k]}; done
    printf '%s%s%s%s\n' "$pad" "$out" "$C_RESET" "$C_EL"
  }

  printf '\n'
  if ((UI_ANIM)); then
    printf '\e[?25l'
    for r in 0 1 2 3 4; do printf '\n'; done
    for ((frame = 4; frame <= width; frame += 4)); do
      printf '\e[5A'
      for r in 0 1 2 3 4; do ui__row "$r" "$frame"; done
      if ui__pause_or_key 0.022; then skip=1; break; fi
    done
    printf '\e[5A'
  fi
  for r in 0 1 2 3 4; do ui__row "$r" "$width"; done
  printf '\n'
  lines=$(((width - ${#word}) / 2))
  printf '%s%*s' "$pad" "$lines" ''
  if ((UI_ANIM && !skip)); then
    for ((c = 1; c <= ${#word}; c += 2)); do
      printf '\r%s%*s%s%s' "$pad" "$lines" '' "$C_BOLD" "$(ui_gradient "${word:0:c}" "${#word}")"
      if ui__pause_or_key 0.018; then skip=1; break; fi
    done
    printf '\r%s%*s' "$pad" "$lines" ''
  fi
  printf '%s%s%s\n' "$C_BOLD" "$(ui_gradient "$word")" "$C_RESET"
  printf '%s%*s%s%s%s\n' "$pad" $(((width - ${#tag}) / 2)) '' "$C_MUTED" "$tag" "$C_RESET"
  ((UI_ANIM)) && printf '\e[?25h'
  return 0
}

# ---- lines of output ----------------------------------------------------------------------

ui_section() {  # ui_section "Warm-up" "Checking your machine"
  local rule
  printf '\n  %s%s%s %s%s%s' "$C_GOLD" "$S_DIAMOND" "$C_RESET" "$C_BOLD" "$(ui_gradient "$1" 24)" "$C_RESET"
  [[ -n ${2:-} ]] && printf '  %s%s%s' "$C_MUTED" "$2" "$C_RESET"
  if ((UI_UTF8)); then ui__repeat '─' 48; else ui__repeat '-' 48; fi; rule=$REPLY
  printf '\n  %s%s%s\n' "$C_DIM" "$rule" "$C_RESET"
}

ui_ok()   { printf '  %s%s%s %s%s\n' "$C_OK" "$S_OK" "$C_RESET" "$1" "${2:+  $C_MUTED$2$C_RESET}"; }
ui_warn() { printf '  %s%s%s %s%s\n' "$C_WARN" "$S_WARN" "$C_RESET" "$1" "${2:+  $C_MUTED$2$C_RESET}"; }
ui_fail() { printf '  %s%s%s %s%s\n' "$C_ERR" "$S_FAIL" "$C_RESET" "$1" "${2:+  $C_MUTED$2$C_RESET}" >&2; }
ui_info() { printf '  %s%s%s %s%s\n' "$C_CYAN" "$S_INFO" "$C_RESET" "$1" "${2:+  $C_MUTED$2$C_RESET}"; }
ui_note() { printf '    %s%s%s\n' "$C_MUTED" "$1" "$C_RESET"; }
ui_kv()   { printf '    %s%-14s%s %s\n' "$C_MUTED" "$1" "$C_RESET" "$2"; }

# Stop with a plain message and, when there is one, what to do about it.
ui_die() {
  printf '\n' >&2
  ui_fail "$1"
  [[ -n ${2:-} ]] && printf '    %s\n' "$2" >&2
  if [[ -n ${OWG_LOG:-} && -s ${OWG_LOG:-} ]]; then printf '    %sFull log: %s%s\n' "$C_MUTED" "$OWG_LOG" "$C_RESET" >&2; fi
  exit "${3:-1}"
}

# A rounded box: ui_box "Title" "line" "line" ... Lines may hold colour.
ui_box() {
  local title=$1 width=0 line h v tl tr bl br pad top
  shift
  for line in "$@"; do ui__width "$line"; ((REPLY > width)) && width=$REPLY; done
  ((${#title} + 4 > width)) && width=$((${#title} + 4))
  width=$((width + 4))
  if ((UI_UTF8)); then h='─' v='│' tl='╭' tr='╮' bl='╰' br='╯'; else h='-' v='|' tl='+' tr='+' bl='+' br='+'; fi
  ui__repeat "$h" $((width - ${#title} - 3)); top=$REPLY
  printf '\n  %s%s%s %s%s%s %s%s%s\n' "$C_GOLD" "$tl$h" "$C_RESET" "$C_BOLD" "$(ui_gradient "$title")" "$C_RESET" "$C_GOLD" "$top$tr" "$C_RESET"
  for line in "" "$@" ""; do
    ui__width "$line"; printf -v pad '%*s' $((width - REPLY - 2)) ''
    printf '  %s%s%s  %s%s%s%s%s\n' "$C_GOLD" "$v" "$C_RESET" "$line" "$C_RESET" "$pad" "$C_GOLD$v" "$C_RESET"
  done
  ui__repeat "$h" "$width"
  printf '  %s%s%s%s%s\n' "$C_GOLD" "$bl" "$REPLY" "$br" "$C_RESET"
}

# ---- working --------------------------------------------------------------------------------

ui__elapsed() {  # REPLY = seconds since $1, as 4s or 2m 05s
  local s=$((SECONDS - $1))
  if ((s >= 60)); then printf -v REPLY '%dm %02ds' $((s / 60)) $((s % 60)); else REPLY="${s}s"; fi
}

ui__log() { printf '%s\n' "$*" >>"${OWG_LOG:-/dev/null}" 2>/dev/null || true; }

# The last lines of the log, shown when a step fails.
ui__tail_log() {  # ui__tail_log [FROM-LINE]
  [[ -n ${OWG_LOG:-} && -s $OWG_LOG ]] || return 0
  printf '    %sLast lines of %s:%s\n' "$C_MUTED" "$OWG_LOG" "$C_RESET" >&2
  tail -n "+$((${1:-0} + 1))" "$OWG_LOG" | tail -n 12 | sed "s/^/      /" >&2
}

ui__log_lines() {  # REPLY = how many lines the log has now
  REPLY=0
  [[ -n ${OWG_LOG:-} && -f $OWG_LOG ]] && REPLY=$(wc -l <"$OWG_LOG")
  return 0
}

# Run a command with the barbell spinner, its output going to the log:
#   ui_step "Installing Python packages" -- pip install ...
# The command must not ask anything: it has no terminal. Returns the command's status.
ui_step() {
  local label=$1 start=$SECONDS pid rc i=0 n mark
  shift; [[ ${1:-} == -- ]] && shift
  ui__log "" "==== $label: $*"
  ui__log_lines; mark=$REPLY
  if ((!UI_ANIM)); then
    printf '  %s%s%s %s%s\n' "$C_MUTED" "$S_INFO" "$C_RESET" "$label" "$S_ELLIPSIS"
    if "$@" >>"${OWG_LOG:-/dev/null}" 2>&1 </dev/null; then rc=0; else rc=$?; fi
  else
    n=${#UI_LIFT[@]}
    "$@" >>"${OWG_LOG:-/dev/null}" 2>&1 </dev/null &
    pid=$! UI_BG_PID=$!
    printf '\e[?25l'
    local colour
    while kill -0 "$pid" 2>/dev/null; do
      ui__gradient_at $((i % 24)) 24; colour=$REPLY
      ui__elapsed "$start"
      printf '\r  %s%s%s %s %s%s%s\e[K' "$colour" "${UI_LIFT[i % n]}" "$C_RESET" "$label" "$C_DIM" "$REPLY" "$C_RESET"
      ui__sleep 0.09
      i=$((i + 1))
    done
    if wait "$pid"; then rc=0; else rc=$?; fi
    printf '\r\e[K\e[?25h'
  fi
  ui__elapsed "$start"
  if ((rc == 0)); then
    ui_ok "$label" "$REPLY"
  else
    ui_fail "$label" "failed after $REPLY"
    ui__tail_log "$mark"
  fi
  return "$rc"
}

ui__bytes() {  # REPLY = $1 bytes, as 790 MB or 1.6 GB
  local n=$1
  if ((n >= 1073741824)); then printf -v REPLY '%d.%d GB' $((n / 1073741824)) $((n % 1073741824 * 10 / 1073741824))
  elif ((n >= 1048576)); then printf -v REPLY '%d MB' $((n / 1048576))
  elif ((n >= 1024)); then printf -v REPLY '%d KB' $((n / 1024))
  else REPLY="$n B"; fi
}

ui__now_ms() {
  if [[ -n ${EPOCHREALTIME:-} ]]; then local t=${EPOCHREALTIME/[.,]/}; REPLY=$((t / 1000)); else REPLY=$((SECONDS * 1000)); fi
}

# Download with a progress bar: ui_download URL FILE "label". The file appears only when complete.
ui_download() {
  local url=$1 dest=$2 label=$3 total size=0 pid rc start=$SECONDS t0 now rate left bar width=28 eighths full part i
  local -a parts=(' ' '▏' '▎' '▍' '▌' '▋' '▊' '▉')
  local filled='#' empty='.'
  ((UI_UTF8)) && filled='█' empty='·'
  total=$(curl -fsSIL --max-time 20 "$url" 2>/dev/null | tr -d '\r' | awk 'tolower($1)=="content-length:"{n=$2} tolower($1)=="x-linked-size:"{x=$2} END{print (x>0?x:n)+0}') || total=0
  [[ $total =~ ^[0-9]+$ ]] || total=0
  mkdir -p "$(dirname "$dest")"
  rm -f "$dest.part"
  ui__log "" "==== $label: $url -> $dest ($total bytes)"
  if ((!UI_ANIM)); then
    ui__bytes "$total"
    printf '  %s%s%s %s%s%s\n' "$C_MUTED" "$S_INFO" "$C_RESET" "$label" "$( ((total)) && printf ' (%s)' "$REPLY")" "$S_ELLIPSIS"
    if curl -fsSL --retry 3 --connect-timeout 20 -o "$dest.part" "$url" >>"${OWG_LOG:-/dev/null}" 2>&1 </dev/null; then rc=0; else rc=$?; fi
  else
    curl -fsSL --retry 3 --connect-timeout 20 -o "$dest.part" "$url" >>"${OWG_LOG:-/dev/null}" 2>&1 </dev/null &
    pid=$! UI_BG_PID=$!
    ui__now_ms; t0=$REPLY
    printf '\e[?25l'
    while kill -0 "$pid" 2>/dev/null; do
      size=$(stat -c %s "$dest.part" 2>/dev/null || echo 0)
      ui__now_ms; now=$REPLY
      rate=$((size * 1000 / ((now - t0) > 0 ? (now - t0) : 1)))
      if ((total > 0)); then
        ((size > total)) && size=$total
        eighths=$((size * width * 8 / total)) full=$((eighths / 8)) part=$((eighths % 8)) bar=''
        for ((i = 0; i < width; i++)); do
          if ((i < full)); then
            ui__gradient_at "$i" "$width"; bar+="$REPLY$filled"
          elif ((i == full && part > 0 && UI_UTF8)); then
            ui__gradient_at "$i" "$width"; bar+="$REPLY${parts[part]}"
          else
            bar+="$C_DIM$empty$C_RESET"
          fi
        done
        ((rate > 0)) && left=$(((total - size) / rate)) || left=0
        ui__bytes "$size"; local got=$REPLY
        ui__bytes "$total"; local all=$REPLY
        ui__bytes "$rate"
        printf '\r  %s %s%s %3d%%  %s%s / %s  %s/s  %d:%02d left%s\e[K' "$label" "$bar" "$C_RESET" $((size * 100 / total)) \
          "$C_MUTED" "$got" "$all" "$REPLY" $((left / 60)) $((left % 60)) "$C_RESET"
      else
        ui__gradient_at $((SECONDS % 24)) 24
        local colour=$REPLY
        ui__bytes "$size"; local got=$REPLY
        ui__bytes "$rate"
        printf '\r  %s%s%s %s  %s%s  %s/s%s\e[K' "$colour" "${UI_LIFT[SECONDS % ${#UI_LIFT[@]}]}" "$C_RESET" "$label" "$C_MUTED" "$got" "$REPLY" "$C_RESET"
      fi
      ui__sleep 0.2
    done
    if wait "$pid"; then rc=0; else rc=$?; fi
    printf '\r\e[K\e[?25h'
  fi
  ui__elapsed "$start"
  if ((rc == 0)); then
    mv -f "$dest.part" "$dest"
    local took=$REPLY
    ui__bytes "$(stat -c %s "$dest" 2>/dev/null || echo 0)"
    ui_ok "$label" "$REPLY in $took"
  else
    rm -f "$dest.part"
    ui_fail "$label" "download failed (curl exit $rc)"
    ui__log "curl exit $rc for $url"
  fi
  return "$rc"
}

# ---- questions ------------------------------------------------------------------------------

ui__read_key() {  # REPLY = up, down, space, enter, a digit, a letter or other
  local key rest
  IFS= read -r -s -n1 key <&"$UI_TTY_FD" || { REPLY=enter; return; }
  case "$key" in
    $'\e')
      IFS= read -r -s -n2 -t 0.05 rest <&"$UI_TTY_FD" || rest=''
      case "$rest" in '[A'|'OA') REPLY=up ;; '[B'|'OB') REPLY=down ;; *) REPLY=other ;; esac ;;
    '') REPLY=enter ;;
    ' ') REPLY=space ;;
    k) REPLY=up ;;
    j) REPLY=down ;;
    *) REPLY=$key ;;
  esac
}

ui__question() {  # the line a question starts with
  printf '\n  %s?%s %s%s%s\n' "$C_GOLD" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"
}

# Pick one: ui_choose VAR "Question" DEFAULT "Label|description" ... VAR is set to the index.
ui_choose() {
  local -n _chosen=$1
  local question=$2 current=${3:-0} count i label desc key widest=0
  shift 3
  local -a options=("$@")
  count=${#options[@]}
  for i in "${!options[@]}"; do label=${options[i]%%|*}; ((${#label} > widest)) && widest=${#label}; done

  if ((!UI_ASK)); then
    _chosen=$current
    ui_ok "$question" "${options[current]%%|*}"
    return 0
  fi
  ui__question "$question"
  if ((!UI_COLOR)); then
    for i in "${!options[@]}"; do
      label=${options[i]%%|*} desc=${options[i]#*|}; [[ $desc == "${options[i]}" ]] && desc=''
      printf '    %d) %-*s  %s\n' $((i + 1)) "$widest" "$label" "$desc"
    done
    while true; do
      printf '  Choose 1-%d [%d]: ' "$count" $((current + 1))
      IFS= read -r key <&"$UI_TTY_FD" || key=''
      [[ -z $key ]] && break
      if [[ $key =~ ^[0-9]+$ ]] && ((key >= 1 && key <= count)); then current=$((key - 1)); break; fi
    done
    _chosen=$current
    return 0
  fi

  printf '\e[?25l'
  local first=1
  while true; do
    ((first)) || printf '\e[%dA' "$count"
    first=0
    for i in "${!options[@]}"; do
      label=${options[i]%%|*} desc=${options[i]#*|}; [[ $desc == "${options[i]}" ]] && desc=''
      if ((i == current)); then
        printf '\r  %s%s %s%-*s%s  %s%s%s\e[K\n' "$C_GOLD" "$S_POINT" "$C_BOLD" "$widest" "$label" "$C_RESET" "$C_MUTED" "$desc" "$C_RESET"
      else
        printf '\r    %s%-*s  %s%s%s\e[K\n' "$C_MUTED" "$widest" "$label" "$C_DIM" "$desc" "$C_RESET"
      fi
    done
    printf '\r  %s↑↓ move · ⏎ choose%s\e[K' "$C_DIM" "$C_RESET"
    ui__read_key; key=$REPLY
    printf '\r\e[K'
    case "$key" in
      up) current=$(((current - 1 + count) % count)) ;;
      down) current=$(((current + 1) % count)) ;;
      enter) break ;;
      [1-9]) if ((key <= count)); then current=$((key - 1)); fi ;;
    esac
  done
  # Fold the list away, leaving the answer.
  printf '\e[%dA' $((count + 1))
  for ((i = 0; i <= count; i++)); do printf '\r\e[K\n'; done
  printf '\e[%dA\r\e[K  %s%s%s %s %s%s%s\n\e[?25h' $((count + 2)) "$C_OK" "$S_OK" "$C_RESET" "$question" "$C_GOLD" "${options[current]%%|*}" "$C_RESET"
  _chosen=$current
}

# Tick any number: ui_checklist VAR "Question" "Label|description|on" ...  VAR = the ticked indexes.
ui_checklist() {
  local -n _ticked=$1
  local question=$2 count i label rest desc state current=0 key widest=0
  shift 2
  local -a options=("$@") on=()
  count=${#options[@]}
  for i in "${!options[@]}"; do
    label=${options[i]%%|*}; ((${#label} > widest)) && widest=${#label}
    state=${options[i]##*|}; [[ $state == on ]] && on[i]=1 || on[i]=0
  done
  if ((!UI_ASK)); then
    _ticked=''
    for i in "${!options[@]}"; do ((on[i])) && _ticked+="$i "; done
    _ticked=${_ticked% }
    return 0
  fi
  ui__question "$question"
  if ((!UI_COLOR)); then
    while true; do
      for i in "${!options[@]}"; do
        label=${options[i]%%|*} rest=${options[i]#*|} desc=${rest%|*}
        printf '    %d) %s %-*s  %s\n' $((i + 1)) "$( ((on[i])) && printf '%s' "$S_ON" || printf '%s' "$S_OFF")" "$widest" "$label" "$desc"
      done
      printf '  Number to toggle, Enter when done: '
      IFS= read -r key <&"$UI_TTY_FD" || key=''
      [[ -z $key ]] && break
      if [[ $key =~ ^[0-9]+$ ]] && ((key >= 1 && key <= count)); then on[key - 1]=$((1 - on[key - 1])); fi
    done
  else
    printf '\e[?25l'
    local first=1
    while true; do
      ((first)) || printf '\e[%dA' "$count"
      first=0
      for i in "${!options[@]}"; do
        label=${options[i]%%|*} rest=${options[i]#*|} desc=${rest%|*}
        local box=$S_OFF colour=$C_MUTED
        ((on[i])) && box=$S_ON colour=$C_OK
        if ((i == current)); then
          printf '\r  %s%s%s %s%s%s %s%-*s%s  %s%s%s\e[K\n' "$C_GOLD" "$S_POINT" "$C_RESET" "$colour" "$box" "$C_RESET" "$C_BOLD" "$widest" "$label" "$C_RESET" "$C_MUTED" "$desc" "$C_RESET"
        else
          printf '\r    %s%s%s %-*s  %s%s%s\e[K\n' "$colour" "$box" "$C_RESET" "$widest" "$label" "$C_DIM" "$desc" "$C_RESET"
        fi
      done
      printf '\r  %s↑↓ move · space tick · a all · n none · ⏎ done%s\e[K' "$C_DIM" "$C_RESET"
      ui__read_key; key=$REPLY
      printf '\r\e[K'
      case "$key" in
        up) current=$(((current - 1 + count) % count)) ;;
        down) current=$(((current + 1) % count)) ;;
        space) on[current]=$((1 - on[current])) ;;
        a) for i in "${!options[@]}"; do on[i]=1; done ;;
        n) for i in "${!options[@]}"; do on[i]=0; done ;;
        enter) break ;;
      esac
    done
    printf '\n\e[?25h'
  fi
  _ticked=''
  for i in "${!options[@]}"; do ((on[i])) && _ticked+="$i "; done
  _ticked=${_ticked% }
  return 0
}

# Yes or no: ui_confirm "Question" [y|n]. Returns 0 for yes.
ui_confirm() {
  local question=$1 default=${2:-y} key hint
  [[ $default == y ]] && hint='Y/n' || hint='y/N'
  if ((!UI_ASK)); then [[ $default == y ]]; return; fi
  printf '\n  %s?%s %s%s%s %s(%s)%s ' "$C_GOLD" "$C_RESET" "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "$hint" "$C_RESET"
  while true; do
    IFS= read -r -s -n1 key <&"$UI_TTY_FD" || key=''
    case "$key" in
      y|Y) key=y; break ;;
      n|N) key=n; break ;;
      '') key=$default; break ;;
    esac
  done
  if [[ $key == y ]]; then printf '%sYes%s\n' "$C_OK" "$C_RESET"; return 0; fi
  printf '%sNo%s\n' "$C_MUTED" "$C_RESET"
  return 1
}

# A line of text: ui_input VAR "Question" "default" [validator-function]
ui_input() {
  local -n _answer=$1
  local question=$2 default=${3:-} check=${4:-} value
  if ((!UI_ASK)); then _answer=$default; return 0; fi
  while true; do
    printf '\n  %s?%s %s%s%s %s%s%s ' "$C_GOLD" "$C_RESET" "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "${default:+($default) }›" "$C_RESET"
    IFS= read -r value <&"$UI_TTY_FD" || value=''
    value=${value:-$default}
    if [[ -z $check ]] || "$check" "$value"; then break; fi
  done
  _answer=$value
}

# A password, typed twice: ui_secret VAR "Question" [minimum length] [once]
# With `once` it is asked a single time, for a key pasted from somewhere else.
ui_secret() {
  local -n _secret=$1
  local question=$2 minimum=${3:-8} once=${4:-} first second
  if ((!UI_ASK)); then _secret=''; return 1; fi
  while true; do
    printf '\n  %s?%s %s%s%s %s›%s ' "$C_GOLD" "$C_RESET" "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "$C_RESET"
    IFS= read -r -s first <&"$UI_TTY_FD" || first=''
    printf '\n'
    if ((${#first} < minimum)); then ui_warn "At least $minimum characters, please."; continue; fi
    [[ $once == once ]] && break
    printf '    %sAgain%s %s›%s ' "$C_MUTED" "$C_RESET" "$C_DIM" "$C_RESET"
    IFS= read -r -s second <&"$UI_TTY_FD" || second=''
    printf '\n'
    if [[ $first != "$second" ]]; then ui_warn "Those two were different. Once more."; continue; fi
    break
  done
  _secret=$first
}

# Ask for sudo once, in the open, so later steps can use it without a prompt behind a spinner.
ui_sudo() {
  local why=$1
  ((EUID == 0)) && return 0
  command -v sudo >/dev/null || ui_die "This needs root ($why), and sudo is not installed." "Run it as root, or install sudo."
  sudo -n true 2>/dev/null && return 0
  ui_info "Your password is needed for: $why"
  if ((UI_IN_TTY)); then sudo -v <&"$UI_TTY_FD" || ui_die "sudo was refused."; else sudo -v || ui_die "sudo was refused."; fi
}

# ---- the finish -----------------------------------------------------------------------------

# The barbell goes up, comes down hard, and the set is done.
ui_finale() {
  local message=${1:-"Set complete"} width=34 heights=() h r frame i spark line col
  if ((!UI_ANIM || !UI_UTF8)); then
    printf '\n  %s%s%s %s%s%s\n' "$C_GOLD" "$S_DIAMOND" "$C_RESET" "$C_BOLD" "$(ui_gradient "$message")" "$C_RESET"
    return 0
  fi
  # Nine heights in three rows of braille: floor, middle and top of each row.
  local -a level=('⣤⣀⣀⣀⣀⣀⣀⣀⣤' '⠶⠤⠤⠤⠤⠤⠤⠤⠶' '⠛⠒⠒⠒⠒⠒⠒⠒⠛')
  local -a sparks=('✦' '✧' '·' '⋆' '✶')
  heights=(0 0 1 2 3 4 5 6 7 8 8 8 8 8 6 3 0 0)
  printf '\e[?25l\n\n\n\n'
  for ((frame = 0; frame < ${#heights[@]} + 10; frame++)); do
    h=${heights[frame]:-0}
    printf '\e[4A'
    for r in 2 1 0; do
      line=''
      printf -v line '%*s' "$width" ''
      if ((r == h / 3)); then
        local bar=${level[h % 3]} at=$(((width - 9) / 2))
        line="${line:0:at}$bar${line:at + 9}"
      fi
      # Sparkles while it is held up, and a burst when it lands.
      if ((frame >= 10 && frame <= 13)) || ((frame >= ${#heights[@]} - 1)); then
        for i in 1 2 3; do
          col=$((RANDOM % width))
          spark=${sparks[RANDOM % ${#sparks[@]}]}
          [[ ${line:col:1} == ' ' ]] && line="${line:0:col}$spark${line:col + 1}"
        done
      fi
      printf '      %s\e[K\n' "$(ui_gradient "$line" "$width")"
    done
    if ((frame >= ${#heights[@]} - 1)); then
      printf '      %s%s%s\e[K\n' "$C_BOLD" "$(ui_gradient "$(printf '%*s' $(((width + ${#message}) / 2)) "$message")" "$width")" "$C_RESET"
    else
      printf '\e[K\n'
    fi
    if ui__pause_or_key 0.05; then break; fi
  done
  printf '\e[?25h'
}

# Open a page in the desktop's browser when there is a desktop here; say so either way.
ui_open_browser() {
  local url=$1
  if [[ -z ${SSH_CONNECTION:-} && ( -n ${DISPLAY:-} || -n ${WAYLAND_DISPLAY:-} ) ]] && command -v xdg-open >/dev/null; then
    (xdg-open "$url" >/dev/null 2>&1 &) && ui_info "Opening $url in your browser"
    return 0
  fi
  return 1
}
