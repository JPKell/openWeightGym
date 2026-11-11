# shellcheck shell=bash disable=SC2034  # variables set here are for the scripts that source this
# What this machine is: its system, package manager, address, Python and graphics cards.
# Shared by install.sh, scripts/install-llamacpp.sh and scripts/owg. Each function sets
# variables and prints nothing; none of them changes the machine.

[[ -n ${OWG_DETECT_LOADED:-} ]] && return 0
OWG_DETECT_LOADED=1

# OS_NAME (for people), OS_ID, OS_FAMILY (arch, debian, fedora, suse or other), OS_ARCH, OS_WSL (0/1).
detect_os() {
  OS_NAME=$(uname -s) OS_ID=unknown OS_FAMILY=other OS_ARCH=$(uname -m) OS_WSL=0
  if [[ -r /etc/os-release ]]; then
    local ID='' ID_LIKE='' PRETTY_NAME=''
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID=${ID:-unknown} OS_NAME=${PRETTY_NAME:-$OS_NAME}
    case " ${ID:-} ${ID_LIKE:-} " in
      *' arch '*|*' archarm '*|*' manjaro '*|*' endeavouros '*|*' cachyos '*) OS_FAMILY=arch ;;
      *' debian '*|*' ubuntu '*) OS_FAMILY=debian ;;
      *' fedora '*|*' rhel '*|*' centos '*|*' rocky '*|*' almalinux '*) OS_FAMILY=fedora ;;
      *' suse '*|*' opensuse '*|*' sles '*|*' opensuse-'*) OS_FAMILY=suse ;;
    esac
  fi
  grep -qi microsoft /proc/version 2>/dev/null && OS_WSL=1
  return 0
}

# PKG_MANAGER (apt, dnf, zypper or pacman; empty when unknown) and PKG_INSTALL: the command, as an
# array, that installs packages with sudo and asks nothing. The family's own manager comes first,
# so a stray copy of another one is never used.
detect_package_manager() {
  PKG_INSTALL=() PKG_MANAGER=''
  local candidate
  local -a order=(apt-get dnf zypper pacman yum)
  case "${OS_FAMILY:-other}" in
    arch) order=(pacman "${order[@]}") ;;
    debian) order=(apt-get "${order[@]}") ;;
    fedora) order=(dnf yum "${order[@]}") ;;
    suse) order=(zypper "${order[@]}") ;;
  esac
  for candidate in "${order[@]}"; do
    command -v "$candidate" >/dev/null || continue
    case "$candidate" in
      pacman) PKG_MANAGER=pacman PKG_INSTALL=(pacman -S --needed --noconfirm) ;;
      apt-get) PKG_MANAGER=apt PKG_INSTALL=(/usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y) ;;
      dnf) PKG_MANAGER=dnf PKG_INSTALL=(dnf install -y) ;;
      yum) PKG_MANAGER=dnf PKG_INSTALL=(yum install -y) ;;
      zypper) PKG_MANAGER=zypper PKG_INSTALL=(zypper --non-interactive install) ;;
    esac
    break
  done
  return 0
}

# The command a person would type to install packages here: pkg_command git cmake
pkg_command() {
  case "$PKG_MANAGER" in
    apt) printf 'sudo apt-get install %s' "$*" ;;
    dnf) printf 'sudo %s install %s' "${PKG_INSTALL[0]:-dnf}" "$*" ;;
    zypper) printf 'sudo zypper install %s' "$*" ;;
    pacman) printf 'sudo pacman -S --needed %s' "$*" ;;
    *) printf "install with your distribution's package manager: %s" "$*" ;;
  esac
}

# What fixes a package manager that cannot find or fetch packages: stale lists, mostly.
pkg_refresh_hint() {
  case "$PKG_MANAGER" in
    apt) printf 'Refresh the package lists: sudo apt-get update' ;;
    dnf) printf 'Refresh the package lists: sudo %s makecache --refresh' "${PKG_INSTALL[0]:-dnf}" ;;
    zypper) printf 'Refresh the repositories: sudo zypper refresh' ;;
    pacman) printf 'Bring the system up to date (Arch installs only against current package lists): sudo pacman -Syu' ;;
    *) printf 'Check that your package manager can install packages' ;;
  esac
}

# The packages that build C++ programs with cmake, as pkg_command takes them.
build_packages() {
  case "$PKG_MANAGER" in
    apt) echo git cmake build-essential ;;
    pacman) echo git cmake base-devel ;;
    *) echo git cmake gcc-c++ make ;;
  esac
}

# Development packages for a source build; callers split the returned package names into an array.
build_dependency_packages() {  # build_dependency_packages tools|openssl|cuda|vulkan|rocm
  case "$PKG_MANAGER:$1" in
    *:tools) build_packages ;;
    apt:openssl) echo pkg-config libssl-dev ;;
    pacman:openssl) echo pkgconf openssl ;;
    dnf:openssl) echo pkgconf-pkg-config openssl-devel ;;
    zypper:openssl) echo pkg-config libopenssl-devel ;;
    apt:cuda) echo nvidia-cuda-toolkit ;;
    pacman:cuda) echo cuda ;;
    dnf:cuda|zypper:cuda) echo cuda-toolkit ;;
    apt:vulkan) echo libvulkan-dev glslc spirv-headers ;;
    pacman:vulkan) echo vulkan-headers vulkan-icd-loader shaderc spirv-headers ;;
    dnf:vulkan) echo vulkan-loader-devel glslc spirv-headers-devel ;;
    zypper:vulkan) echo vulkan-devel shaderc spirv-headers-devel ;;
    apt:rocm) echo hipcc libhipblas-dev librocblas-dev ;;
    pacman:rocm) echo hip-runtime-amd hipblas rocblas ;;
    dnf:rocm) echo rocm-hip-devel hipblas-devel rocblas-devel ;;
    zypper:rocm) echo hip-devel hipblas-devel rocblas-devel ;;
    *) return 1 ;;
  esac
}

# The distribution's package for a program or library this installer may need.
package_for() {  # package_for git|libgomp.so.1|libvulkan.so.1|libssl.so.3 ...
  local what=$1
  case "$PKG_MANAGER:$what" in
    *:git) echo git ;;
    *:curl) echo curl ;;
    *:tar) echo tar ;;
    *:gzip) echo gzip ;;
    *:age) echo age ;;
    *:awk) echo gawk ;;
    *:sed) echo sed ;;
    *:grep) echo grep ;;
    apt:polkit) echo polkitd ;;
    *:polkit) echo polkit ;;
    apt:libgomp.so.1) echo libgomp1 ;;
    dnf:libgomp.so.1) echo libgomp ;;
    zypper:libgomp.so.1) echo libgomp1 ;;
    pacman:libgomp.so.1) echo gcc-libs ;;
    apt:libvulkan.so.1) echo libvulkan1 ;;
    dnf:libvulkan.so.1) echo vulkan-loader ;;
    zypper:libvulkan.so.1) echo libvulkan1 ;;
    pacman:libvulkan.so.1) echo vulkan-icd-loader ;;
    apt:libssl.so.3|apt:libcrypto.so.3) echo libssl3 ;;
    dnf:libssl.so.3|dnf:libcrypto.so.3) echo openssl-libs ;;
    zypper:libssl.so.3|zypper:libcrypto.so.3) echo libopenssl3 ;;
    pacman:libssl.so.3|pacman:libcrypto.so.3) echo openssl ;;
    apt:libstdc++.so.6) echo libstdc++6 ;;
    dnf:libstdc++.so.6) echo libstdc++ ;;
    zypper:libstdc++.so.6) echo libstdc++6 ;;
    pacman:libstdc++.so.6) echo gcc-libs ;;
    *) return 1 ;;
  esac
}

# Whether a TCP port is in a list of ports and ranges: port_listed 8442 "1025-65535/tcp 80,443 8440:8450"
port_listed() {
  awk -v port="$1" '{
    for (i = 1; i <= NF; i++) {
      n = split($i, a, "/"); if (n > 1 && a[2] != "tcp") continue
      m = split(a[1], list, ",")
      for (j = 1; j <= m; j++) {
        delete r; split(list[j], r, /[-:]/); if (r[2] == "") r[2] = r[1]
        if (r[1] ~ /^[0-9]+$/ && port + 0 >= r[1] + 0 && port + 0 <= r[2] + 0) found = 1
      }
    }
  } END { exit !found }' <<<"$2"
}

# The TCP ports ufw lets in, from its rules file when that is readable (it is on some systems).
ufw_rules_ports() {
  awk '$1 == "-A" && $2 == "ufw-user-input" && / -j (ACCEPT|ufw-user-limit-accept)( |$)/ {
    proto = "all"; ports = ""
    for (i = 3; i < NF; i++) { if ($i == "-p") proto = $(i + 1); if ($i == "--dport" || $i == "--dports") ports = $(i + 1) }
    if (proto != "tcp" && proto != "all") next
    print (ports == "" ? "1-65535" : ports)
  }' "${1:-/etc/ufw/user.rules}"
}

# FIREWALL_STEPS: how to let other machines reach these TCP ports, when a firewall that is on
# keeps them out (or may: FIREWALL_SURE=0 when its rules could not be read). FIREWALL_NAME names
# that firewall and FIREWALL_CLOSED the ports. Fedora, RHEL and openSUSE turn firewalld on as they
# install; Ubuntu leaves ufw off unless someone turns it on.
firewall_steps() {  # firewall_steps 8440 8442
  FIREWALL_STEPS=() FIREWALL_NAME='' FIREWALL_CLOSED='' FIREWALL_SURE=1
  FIREWALL_STATUS=inactive FIREWALL_MESSAGE='No active UFW or firewalld firewall detected.'
  local port listing='' status='' known=1 closed=() rules=()
  (($#)) || { FIREWALL_STATUS=local; FIREWALL_MESSAGE='No externally bound TCP ports need firewall rules.'; return 0; }
  if command -v firewall-cmd >/dev/null && [[ $(firewall-cmd --state 2>/dev/null) == running ]]; then
    FIREWALL_NAME=firewalld
    listing=$(firewall-cmd --list-ports 2>/dev/null || sudo -n firewall-cmd --list-ports 2>/dev/null) || known=0
  elif command -v ufw >/dev/null; then
    FIREWALL_NAME=ufw
    # A rule allowing everything from somewhere (Anywhere ALLOW 192.168.1.0/24) lets these in too.
    if status=$(LC_ALL=C ufw status 2>/dev/null || LC_ALL=C sudo -n ufw status 2>/dev/null); then
      [[ $status == *'Status: active'* ]] || return 0
      listing=$(awk '$2 ~ /^(ALLOW|LIMIT)$/ || $3 ~ /^(ALLOW|LIMIT)$/ {print ($1 == "Anywhere" ? "1-65535" : $1)}' <<<"$status")
    elif grep -qs '^ENABLED=no' /etc/ufw/ufw.conf; then
      return 0
    elif grep -qs '^ENABLED=yes' /etc/ufw/ufw.conf && [[ -r /etc/ufw/user.rules ]]; then
      listing=$(ufw_rules_ports) || known=0
    else
      known=0
    fi
  else
    return 0
  fi
  for port; do
    if ((known)) && port_listed "$port" "$listing"; then continue; fi
    closed+=("$port")
  done
  if ((${#closed[@]} == 0)); then
    FIREWALL_STATUS=clear
    FIREWALL_MESSAGE="Firewall ($FIREWALL_NAME) has allow rules for required TCP ports: $*."
    return 0
  fi
  FIREWALL_CLOSED=${closed[*]} FIREWALL_SURE=$known
  FIREWALL_STATUS=blocked
  FIREWALL_MESSAGE="Firewall ($FIREWALL_NAME) needs allow rules for required TCP ports: $FIREWALL_CLOSED."
  if ((!known)); then
    FIREWALL_STATUS=unknown
    FIREWALL_MESSAGE="Firewall ($FIREWALL_NAME) rules could not be read; check required TCP ports: $FIREWALL_CLOSED."
  fi
  case "$FIREWALL_NAME" in
    firewalld)
      for port in "${closed[@]}"; do rules+=("--add-port=$port/tcp"); done
      ((known)) || FIREWALL_STEPS=("Inspect its active rules: sudo firewall-cmd --list-all")
      FIREWALL_STEPS+=("Open $( ((${#closed[@]} > 1)) && echo them || echo it) for good: sudo firewall-cmd --permanent ${rules[*]} && sudo firewall-cmd --reload") ;;
    ufw)
      for port in "${closed[@]}"; do rules+=("sudo ufw allow $port/tcp"); done
      ((known)) || FIREWALL_STEPS=("See whether it does: sudo ufw status")
      FIREWALL_STEPS+=("Open $( ((${#closed[@]} > 1)) && echo them || echo it): $(printf '%s && ' "${rules[@]}" | sed 's/ && $//')") ;;
  esac
  return 0
}

# Shared by the guided installer, direct installer and owg doctor. Only port numbers leave the settings reader.
firewall_installation() {  # ROOT SERVICE,... PYTHON [COMMAND-PREFIX...]
  local root=$1 services=$2 python=$3 ports
  shift 3
  if ! ports=$("$@" "$python" "$root/scripts/install-suite-support.py" firewall-ports --root "$root" --services "$services"); then
    FIREWALL_STATUS=unknown FIREWALL_SURE=0
    FIREWALL_MESSAGE='Firewall check could not read the configured service listeners.'
    FIREWALL_STEPS=("Fix the settings errors above, then run: owg doctor")
    return 0
  fi
  local -a required=()
  if [[ -n $ports ]]; then mapfile -t required <<<"$ports"; fi
  firewall_steps "${required[@]}"
}

firewall_check() {  # ROOT SERVICE,... PYTHON [COMMAND-PREFIX...]
  firewall_installation "$@"
  if ((${#FIREWALL_STEPS[@]})); then ui_attention "$FIREWALL_MESSAGE" "${FIREWALL_STEPS[@]}"
  else ui_ok "$FIREWALL_MESSAGE"; fi
}

# HOST_ADDRESS: this machine's address on its network, the one others reach it by.
detect_address() {
  HOST_ADDRESS=''
  if command -v ip >/dev/null; then
    HOST_ADDRESS=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}') || true
  fi
  # Debian's hostname knows -I; others do not, but every glibc can look up its own name.
  if [[ -z $HOST_ADDRESS ]] && command -v hostname >/dev/null; then
    HOST_ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}') || true
  fi
  if [[ -z $HOST_ADDRESS ]]; then
    HOST_ADDRESS=$(getent ahostsv4 "$(uname -n)" 2>/dev/null | awk '$1 !~ /^127\./ {print $1; exit}') || true
  fi
  [[ -n $HOST_ADDRESS ]] || HOST_ADDRESS=127.0.0.1
  return 0
}

# The systemd user units an installation writes, under every name one has used.
OWG_UNIT_NAMES=(loadcoach weightroom turnstile toollocker spotter voice voice-gateway frontdesk tooldoor)

# What already stands where an installation in ROOT puts its services (default: your own unit folder):
#   UNITS_DIR                      the folder looked in
#   UNITS_OTHER, UNITS_OTHER_ROOT  units of these names that run another installation, and its folder
#   UNITS_DROPINS                  drop-ins that change what a unit not running ROOT runs (its code,
#                                  libraries or command): they apply to any unit of that name
#   UNITS_MASKED                   units switched off by a link to /dev/null
detect_units() {  # detect_units ROOT [UNITS_DIR]
  local root=$1 unit file command conf
  UNITS_DIR=${2:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
  UNITS_OTHER=() UNITS_DROPINS=() UNITS_MASKED=() UNITS_OTHER_ROOT=''
  for unit in "${OWG_UNIT_NAMES[@]}"; do
    file=$UNITS_DIR/$unit.service
    if [[ -L $file && $(readlink "$file") == /dev/null ]]; then
      UNITS_MASKED+=("$unit")
    elif [[ -f $file ]]; then
      command=$(sed -n 's/^ExecStart=//p' "$file" | head -1)
      command=${command//%h/$HOME}
      command=${command#\"}
      [[ $command == "$root"/.venv/bin/* ]] && continue
      if [[ $command == */.venv/bin/* ]]; then
        UNITS_OTHER+=("$unit")
        [[ -n $UNITS_OTHER_ROOT ]] || UNITS_OTHER_ROOT=${command%%/.venv/bin/*}
      fi
    fi
    for conf in "$UNITS_DIR/$unit.service.d"/*.conf; do
      [[ -f $conf ]] || continue
      if grep -qE '^[[:space:]]*(ExecStart|WorkingDirectory|EnvironmentFile)=|PYTHONPATH|LD_LIBRARY_PATH' "$conf"; then
        UNITS_DROPINS+=("$conf")
      fi
    done
  done
  return 0
}

# A path as a person would type it: ~/... when that needs no quoting, otherwise quoted.
shell_path() {
  local rest=${1#"$HOME"/}
  if [[ $1 == "$HOME"/* && $rest =~ ^[A-Za-z0-9._/+@-]+$ ]]; then printf '~/%s' "$rest"; else printf '%q' "$1"; fi
}

# UNITS_STEPS: the commands that stop UNITS_OTHER and move it and UNITS_DROPINS aside, for a person
# to run. Nothing is deleted, and the folder they go to is one systemd does not read.
units_retire_steps() {
  local unit conf aside units=() moves=() seen=' '
  aside=$(dirname "$UNITS_DIR")/retired-units
  for unit in "${UNITS_OTHER[@]}"; do
    units+=("$unit.service")
    moves+=("$(shell_path "$UNITS_DIR/$unit.service")")
    [[ -d $UNITS_DIR/$unit.service.d ]] && { moves+=("$(shell_path "$UNITS_DIR/$unit.service.d")"); seen+="$unit.service.d "; }
  done
  for conf in "${UNITS_DROPINS[@]}"; do
    conf=$(dirname "$conf")
    [[ $seen == *" ${conf##*/} "* ]] && continue
    seen+="${conf##*/} "
    moves+=("$(shell_path "$conf")")
  done
  UNITS_STEPS=()
  ((${#units[@]})) && UNITS_STEPS+=("Stop its services: systemctl --user disable --now ${units[*]}")
  UNITS_STEPS+=("Move the unit files and drop-ins aside (nothing is deleted):
mkdir -p $(shell_path "$aside") && mv ${moves[*]} $(shell_path "$aside")/")
  UNITS_STEPS+=("Make systemd forget them: systemctl --user daemon-reload")
}

# PYTHON: a Python 3.13 or newer with a working venv module, or empty.
detect_python() {
  PYTHON='' PYTHON_VERSION=''
  local candidate
  for candidate in python3.15 python3.14 python3.13 python3; do
    command -v "$candidate" >/dev/null || continue
    if "$candidate" -c 'import sys, venv, ensurepip; raise SystemExit(sys.version_info < (3, 13))' 2>/dev/null; then
      PYTHON=$(command -v "$candidate")
      PYTHON_VERSION=$("$candidate" -c 'import platform; print(platform.python_version())')
      return 0
    fi
  done
  return 0
}

# SYSTEMD_USER: 1 when this account's systemd user manager answers. LINGER: 1 when it outlives logins.
detect_systemd() {
  SYSTEMD_USER=0 LINGER=0 SYSTEMD=0
  command -v systemctl >/dev/null || return 0
  [[ -d /run/systemd/system ]] && SYSTEMD=1
  systemctl --user show-environment >/dev/null 2>&1 && SYSTEMD_USER=1
  [[ $(loginctl show-user "${USER:-$(id -un)}" -p Linger --value 2>/dev/null) == yes ]] && LINGER=1
  return 0
}

# GPU_KIND (nvidia, amd, intel or none), GPU_NAME, GPU_COUNT, GPU_VRAM_MB (the largest card),
# GPU_CUDA (the CUDA version the NVIDIA driver supports, such as 13.0), and LLAMA_BACKEND: the
# prebuilt llama.cpp to use here (cuda-13.4, cuda-12.8, rocm, vulkan or cpu).
detect_gpu() {
  GPU_KIND=none GPU_NAME='' GPU_COUNT=0 GPU_VRAM_MB=0 GPU_CUDA='' LLAMA_BACKEND=cpu
  local line name memory kind_helper
  kind_helper=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)/loadcoach/gpu_type.py
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    GPU_KIND=nvidia
    while IFS=, read -r name memory; do
      [[ -n $name ]] || continue
      if [[ -r $kind_helper ]] && command -v python3 >/dev/null &&
        [[ $(python3 "$kind_helper" --name "$name" 2>/dev/null) == integrated ]]; then continue; fi
      GPU_COUNT=$((GPU_COUNT + 1))
      [[ -z $GPU_NAME ]] && GPU_NAME=${name# }
      memory=${memory//[!0-9]/}
      ((${memory:-0} > GPU_VRAM_MB)) && GPU_VRAM_MB=$memory
    done < <(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null)
    if ((GPU_COUNT > 0)); then
      # Drivers from 610 on say "CUDA UMD Version"; older ones "CUDA Version".
      GPU_CUDA=$(nvidia-smi 2>/dev/null | sed -nE 's/.*CUDA (UMD )?Version: *([0-9][0-9.]*).*/\2/p' | head -1)
      local major=${GPU_CUDA%%.*} minor=${GPU_CUDA#*.}
      minor=${minor%%.*}
      if ((${major:-0} >= 13)); then LLAMA_BACKEND=cuda-13.4
      elif ((${major:-0} == 12 && ${minor:-0} >= 8)); then LLAMA_BACKEND=cuda-12.8
      else LLAMA_BACKEND=vulkan
      fi
      return 0
    fi
    GPU_KIND=none
  fi
  # Use the same integrated/discrete classification as the worker, before Python setup.
  local vendor device first_pci=''
  for line in "${1:-/sys/class/drm}"/card[0-9]*/device/vendor; do
    [[ -r $line ]] || continue
    vendor=$(<"$line")
    device=${line%/vendor}
    if [[ -r $device/device && $vendor == 0x1002 && $(<"$device/device") == 0x164e ]]; then continue; fi
    if [[ -r $kind_helper ]] && command -v python3 >/dev/null &&
      [[ $(python3 "$kind_helper" "$device" 2>/dev/null) == integrated ]]; then continue; fi
    case "$vendor" in
      0x1002) [[ $GPU_KIND == none ]] && GPU_KIND=amd ;;
      0x8086) [[ $GPU_KIND == none ]] && GPU_KIND=intel ;;
      *) continue ;;
    esac
    GPU_COUNT=$((GPU_COUNT + 1))
    [[ -n $first_pci ]] || first_pci=$(basename -- "$(readlink -f -- "$device")")
  done
  if [[ $GPU_KIND != none ]] && command -v lspci >/dev/null; then
    GPU_NAME=$(lspci -s "$first_pci" 2>/dev/null | sed 's/^[^ ]* [^:]*: //')
  fi
  case "$GPU_KIND" in
    amd) if [[ -d /opt/rocm ]]; then LLAMA_BACKEND=rocm; else LLAMA_BACKEND=vulkan; fi ;;
    intel) LLAMA_BACKEND=vulkan ;;
  esac
  return 0
}

# DISK_FREE_MB: free space where $1 is, or would be.
detect_disk() {
  local where=$1
  while [[ ! -e $where && $where != / ]]; do where=$(dirname "$where"); done
  DISK_FREE_MB=$(df -Pm "$where" 2>/dev/null | awk 'NR == 2 {print $4}')
  [[ $DISK_FREE_MB =~ ^[0-9]+$ ]] || DISK_FREE_MB=0
  return 0
}

# Why a step failed, from what it wrote: diagnose_log TEXT sets DIAGNOSIS (a sentence for each
# cause found, two at most) and DIAGNOSIS_STEPS (what puts them right, as commands for this
# system). Only causes the text makes plain are named; for anything else the log speaks for itself.
diagnose_log() {
  local diagnose_text=$1 user line path port host='' extra='' stale='' keys='' program
  local -a fix=()
  DIAGNOSIS=() DIAGNOSIS_STEPS=()
  [[ -n $diagnose_text ]] || return 0
  [[ -v PKG_INSTALL ]] || { detect_os; detect_package_manager; }
  user=$(id -un)

  if diagnose__has 'No space left on device|Disk quota exceeded|\bENOSPC\b|\[Errno 28\]'; then
    diagnose__cause 'The disk is full ("No space left on device").' \
      'See how much room each drive has: df -h' \
      'Make room on the full one: the download caches ~/.cache/uv and ~/.cache/pip are safe to delete, and models are the largest files'
  fi
  if diagnose__has 'MemoryError|Cannot allocate memory|out of memory|std::bad_alloc|^Killed|[0-9]+ Killed |(exit|status|code) 137\b'; then
    diagnose__cause 'The machine ran out of memory, so the system stopped the work.' \
      'Close what uses a lot of memory (a running model, a browser), then carry on' \
      'See what was stopped: sudo dmesg | grep -i "out of memory"'
  fi
  if diagnose__has 'Could not get lock|Unable to acquire the dpkg frontend lock|unable to lock database|System management is locked|Waiting for process with pid|holding the yum lock|Failed to obtain the transaction lock'; then
    case "$PKG_MANAGER" in
      apt) stale='If nothing else is installing, finish the update that was cut short: sudo dpkg --configure -a' ;;
      pacman) stale='If nothing else is installing, the lock was left by an update that stopped: sudo rm /var/lib/pacman/db.lck' ;;
    esac
    diagnose__cause 'Another program is installing or updating software at the same time.' \
      'Let it finish (often an automatic update or the software centre); this shows it: ps -ef | grep -E "apt|dpkg|dnf|yum|zypper|pacman|packagekit" | grep -v grep' \
      "$stale"
  fi
  if diagnose__has 'dpkg was interrupted'; then
    diagnose__cause 'An earlier package installation was cut short.' 'Finish it: sudo dpkg --configure -a'
  fi
  if diagnose__has 'Read-only file system|\bEROFS\b'; then
    diagnose__cause 'A folder it had to change is on a read-only file system.' \
      'On an image-based system (Fedora Silverblue or Kinoite, openSUSE Aeon or MicroOS, SteamOS) system packages are added another way: rpm-ostree install PACKAGE, or sudo transactional-update pkg install PACKAGE, then restart' \
      'Otherwise, see which drives are read-only: findmnt -O ro'
  fi
  if diagnose__has 'is not in the sudoers file|may not run sudo|is not allowed to execute'; then
    diagnose__cause "This account ($user) may not use sudo, which installing system packages needs." \
      'Ask whoever looks after this machine to add you to the sudo or wheel group, or to install the packages named above for you'
  elif diagnose__has 'sudo: a password is required|sudo: a terminal is required|sudo: no tty present'; then
    diagnose__cause 'A step needed sudo, and could not ask for your password.' 'Unlock sudo first: sudo -v'
  fi
  if diagnose__has 'PGP signature|signature from .* is (unknown trust|invalid|marginal)|NO_PUBKEY|EXPKEYSIG|GPG check FAILED|public key for .* is not installed|Signature verification failed'; then
    case "$PKG_MANAGER" in
      pacman) keys='Renew the signing keys, then bring the system up to date: sudo pacman -Sy archlinux-keyring && sudo pacman -Syu' ;;
      apt) keys='A software source in /etc/apt/sources.list.d has a missing or expired key (NO_PUBKEY or EXPKEYSIG above): renew its key or remove that source, then: sudo apt-get update' ;;
      dnf) keys="Bring the system up to date, which renews the keys: sudo ${PKG_INSTALL[0]:-dnf} upgrade --refresh" ;;
      zypper) keys='Refresh the repositories and accept their new keys: sudo zypper refresh' ;;
      *) keys="Renew your package manager's signing keys" ;;
    esac
    diagnose__cause "The package manager could not check a package's signature: its signing keys are missing or out of date." "$keys"
  fi
  if diagnose__has 'certificate verify failed|SSL certificate problem|unable to get local issuer certificate|invalid peer certificate|UnknownIssuer|self[- ]signed certificate|certificate has expired'; then
    diagnose__cause 'A secure download was refused: its certificate could not be checked.' \
      "Install or renew the system's certificates: $(pkg_command ca-certificates)" \
      'Check the clock is right, since a wrong date breaks certificates: date' \
      "On a network that inspects secure traffic (some offices and schools), add its certificate to the system's trusted ones"
  fi
  # Package lists that name files the mirror no longer has: refreshing them is the cure, not the network.
  if diagnose__has 'Failed to fetch.*404|Status code: 404|failed retrieving file.*404|404.*\.(deb|rpm|pkg\.tar)|not found on medium'; then
    diagnose__cause "The package lists here are out of date: they name files the mirror no longer has." "$(pkg_refresh_hint)"
  fi
  if diagnose__has 'Could not resolve|Temporary failure (in name )?resolving|Temporary failure in name resolution|Name or service not known|No address associated with hostname|Network is unreachable|No route to host|Connection timed out|Operation timed out|timed out after|Failed to connect to|Failed to establish a new connection|Connection reset by peer|ConnectTimeout|ReadTimeout|dns error|error sending request|Could not connect to|Cannot retrieve repository metadata|Could not retrieve mirrorlist|Curl error \([0-9]+\)'; then
    # The server it was after: named on the failing lines, or the last address the step used.
    line=$(grep -iE 'resolv|unreachable|timed out|connect|dns|request|retrieve|Curl error' <<<"$diagnose_text" | tail -1) || true
    if [[ $line =~ (https?://|Could not resolve host:? |resolving \'|resolve \')([A-Za-z0-9-]+\.[A-Za-z0-9.-]+) ]]; then host=${BASH_REMATCH[2]}
    elif [[ $(grep -oE 'https?://[A-Za-z0-9-]+\.[A-Za-z0-9.-]+' <<<"$diagnose_text" | tail -1) =~ //(.+)$ ]]; then host=${BASH_REMATCH[1]}
    fi
    host=${host%.}
    diagnose__cause "This machine could not reach ${host:-a server it needed}: the network is down, or something blocks it." \
      "Check the connection: curl -sSI https://${host:-github.com} | head -1 (a working one answers with a line such as HTTP/2 200)" \
      'Behind a proxy? Tell the installer about it: export https_proxy=http://PROXY:PORT http_proxy=http://PROXY:PORT'
  fi
  if diagnose__has 'HTTP (Error )?429|429 Too Many Requests|rate limit exceeded'; then
    diagnose__cause 'A server is limiting how often this machine may download from it.' 'Wait a few minutes: the limit lifts by itself'
  fi
  if diagnose__has 'Unable to locate package|has no installation candidate|No match for argument|error: target not found|No provider of|nothing provides|Unable to find a match'; then
    case "$OS_ID" in
      rocky|almalinux|rhel|centos|ol) extra="On $OS_NAME some packages come from EPEL and CRB: sudo dnf install epel-release && sudo crb enable" ;;
      ubuntu) extra='On Ubuntu some come from the universe component: sudo add-apt-repository universe' ;;
    esac
    diagnose__cause "A package was not found in this system's software sources." "$(pkg_refresh_hint)" "$extra"
  fi
  if diagnose__has "\b(gcc|cc|g\+\+|c\+\+): (command )?not found|unable to execute '(gcc|cc|g\+\+|c\+\+)'|No CMAKE_(C|CXX)_COMPILER could be found|C compiler cannot create executables|linker .cc. not found|Could not find a (C|C\+\+) compiler|error: command '[^']*' failed: No such file or directory"; then
    # shellcheck disable=SC2046  # build_packages is a list of package names
    diagnose__cause 'Building it needs a C and C++ compiler, and none is installed.' "Install the build tools: $(pkg_command $(build_packages))"
  elif line=$(grep -m1 -oE "[A-Za-z0-9._+-]+: command not found|sh: [0-9]+: [A-Za-z0-9._+-]+: not found|env: '[A-Za-z0-9._+-]+': No such file|No such file or directory: '[A-Za-z0-9._+-]+'" <<<"$diagnose_text"); then
    program=${line%: command not found}
    program=${program%: not found}
    program=${program##*: }
    program=${program//\'/}
    program=${program%: No such file}
    program=${program#env: }
    diagnose__cause "The program $program is not installed here." \
      "Install it (its package is usually named the same): $(pkg_command "$(package_for "$program" || printf '%s' "$program")")"
  fi
  if diagnose__has 'No interpreter found for Python 3\.13|No download found for request|python3\.13: (command )?not found'; then
    diagnose__cause 'Python 3.13, which openWeightGym needs, was not found here and could not be fetched.' \
      'uv fetches it from github.com: check this machine can reach it: curl -sSI https://github.com | head -1' \
      'Or install Python 3.13 with your package manager, then run this again'
  fi
  if diagnose__has 'Permission denied|\[Errno 13\]|\bEACCES\b'; then
    path=''
    line=$(grep -m1 -iE 'Permission denied|\[Errno 13\]|EACCES' <<<"$diagnose_text") || true
    if [[ $line =~ \'(/[^\']+)\' ]]; then path=${BASH_REMATCH[1]}
    elif [[ $line =~ (/[^[:space:]:\']+):\ Permission\ denied ]]; then path=${BASH_REMATCH[1]}
    fi
    if [[ -n $path ]]; then
      fix=("See who owns it: ls -ld $(shell_path "$path")")
      [[ $path == "$HOME"/* ]] && fix+=("If root owns it (left by an earlier run with sudo), give it back: sudo chown -R $user: $(shell_path "$path")")
    else
      fix=('The log names the file: see who owns it with ls -ld FILE')
    fi
    diagnose__cause "A file or folder could not be used${path:+ ($(shell_path "$path"))}: it belongs to another account, or its permissions keep $user out." \
      "${fix[@]}"
  fi
  if diagnose__has 'Failed to connect to (user scope )?bus|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR (is )?(not set|invalid)'; then
    diagnose__cause "systemd for this account ($user) could not be reached: that happens after su, sudo -i or some remote log-ins." \
      "Log in as $user directly, or over SSH, rather than switching to it with su" \
      "Or keep its services running without a log-in: sudo loginctl enable-linger $user"
  fi
  if line=$(grep -m1 -iE 'address already in use|EADDRINUSE|\[Errno 98\]' <<<"$diagnose_text"); then
    port=''
    # The port as Python (', 8440)') and Go (':8440:') name it; never an error number.
    line=$(sed -E 's/\[[Ee]rrno [0-9]+\]//g' <<<"$line")
    [[ $line =~ [:,][[:space:]]*([0-9]{2,5})[^0-9]*[Aa]ddress\ already\ in\ use ]] && port=${BASH_REMATCH[1]}
    diagnose__cause "A port openWeightGym needs${port:+ ($port)} is taken by another program." \
      "See which program has it: sudo ss -ltnp${port:+ \"sport = :$port\"}" \
      "If it is an older openWeightGym (or suite) still running, stop that one: systemctl --user list-units --type=service | grep -E 'loadcoach|toollocker|spotter|weightroom|turnstile|voice'"
  fi
  if diagnose__has 'password authentication failed for user|Peer authentication failed|Ident authentication failed|no pg_hba\.conf entry|connection to server .* failed|could not connect to server|Is the server running'; then
    diagnose__cause 'PostgreSQL is not running, or refused the sign-in.' \
      'Check it is running: sudo systemctl status postgresql' \
      "Check its own account can sign in: sudo -u postgres psql -c 'SELECT 1'"
  fi
  return 0
}

# The parts of diagnose_log, which read its diagnose_text.
diagnose__has() { grep -qiE -- "$1" <<<"$diagnose_text"; }
diagnose__cause() {  # diagnose__cause SENTENCE STEP...: the first two causes found are kept
  ((${#DIAGNOSIS[@]} < 2)) || return 0
  DIAGNOSIS+=("$1")
  shift
  local step
  for step in "$@"; do [[ -n $step ]] && DIAGNOSIS_STEPS+=("$step"); done
  return 0
}
