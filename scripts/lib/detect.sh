# shellcheck shell=bash disable=SC2034  # variables set here are for the scripts that source this
# What this machine is: its system, package manager, address, Python and graphics cards.
# Shared by install.sh, scripts/install-llamacpp.sh and scripts/owg. Each function sets
# variables and prints nothing; none of them changes the machine.

[[ -n ${OWG_DETECT_LOADED:-} ]] && return 0
OWG_DETECT_LOADED=1

# OS_NAME (for people), OS_ID, OS_FAMILY (arch, debian, fedora or other), OS_ARCH, OS_WSL (0/1).
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
    esac
  fi
  grep -qi microsoft /proc/version 2>/dev/null && OS_WSL=1
  return 0
}

# PKG_INSTALL: the command, as an array, that installs packages with sudo; empty when unknown.
detect_package_manager() {
  PKG_INSTALL=() PKG_MANAGER=''
  if command -v pacman >/dev/null; then PKG_MANAGER=pacman PKG_INSTALL=(pacman -S --needed --noconfirm)
  elif command -v apt-get >/dev/null; then PKG_MANAGER=apt PKG_INSTALL=(env DEBIAN_FRONTEND=noninteractive apt-get install -y)
  elif command -v dnf >/dev/null; then PKG_MANAGER=dnf PKG_INSTALL=(dnf install -y)
  fi
  return 0
}

# The distribution's package for a program or library this installer may need.
package_for() {  # package_for git|libgomp.so.1|libvulkan.so.1|libssl.so.3 ...
  local what=$1
  case "$PKG_MANAGER:$what" in
    *:git) echo git ;;
    *:curl) echo curl ;;
    *:tar) echo tar ;;
    apt:libgomp.so.1) echo libgomp1 ;;
    dnf:libgomp.so.1) echo libgomp ;;
    pacman:libgomp.so.1) echo gcc-libs ;;
    apt:libvulkan.so.1) echo libvulkan1 ;;
    dnf:libvulkan.so.1) echo vulkan-loader ;;
    pacman:libvulkan.so.1) echo vulkan-icd-loader ;;
    apt:libssl.so.3|apt:libcrypto.so.3) echo libssl3 ;;
    dnf:libssl.so.3|dnf:libcrypto.so.3) echo openssl-libs ;;
    pacman:libssl.so.3|pacman:libcrypto.so.3) echo openssl ;;
    apt:libstdc++.so.6) echo libstdc++6 ;;
    dnf:libstdc++.so.6) echo libstdc++ ;;
    pacman:libstdc++.so.6) echo gcc-libs ;;
    *) return 1 ;;
  esac
}

# HOST_ADDRESS: this machine's address on its network, the one others reach it by.
detect_address() {
  HOST_ADDRESS=''
  if command -v ip >/dev/null; then
    HOST_ADDRESS=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}') || true
  fi
  if [[ -z $HOST_ADDRESS ]] && command -v hostname >/dev/null; then
    HOST_ADDRESS=$(hostname -I 2>/dev/null | awk '{print $1}') || true
  fi
  [[ -n $HOST_ADDRESS ]] || HOST_ADDRESS=127.0.0.1
  return 0
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
  local line name memory
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    GPU_KIND=nvidia
    while IFS=, read -r name memory; do
      [[ -n $name ]] || continue
      GPU_COUNT=$((GPU_COUNT + 1))
      [[ -z $GPU_NAME ]] && GPU_NAME=${name# }
      memory=${memory//[!0-9]/}
      ((${memory:-0} > GPU_VRAM_MB)) && GPU_VRAM_MB=$memory
    done < <(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null)
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
  # Other cards, by the PCI vendor of each display device.
  local vendor
  for line in /sys/class/drm/card[0-9]*/device/vendor; do
    [[ -r $line ]] || continue
    vendor=$(<"$line")
    case "$vendor" in
      0x1002) [[ $GPU_KIND == none ]] && GPU_KIND=amd; GPU_COUNT=$((GPU_COUNT + 1)) ;;
      0x8086) [[ $GPU_KIND == none ]] && GPU_KIND=intel ;;
    esac
  done
  if [[ $GPU_KIND != none ]] && command -v lspci >/dev/null; then
    GPU_NAME=$(lspci 2>/dev/null | grep -iE 'vga|3d|display' | grep -iE "$([[ $GPU_KIND == amd ]] && echo 'amd|ati|radeon' || echo intel)" | head -1 | sed 's/^[^:]*: //')
  fi
  case "$GPU_KIND" in
    amd) if [[ -d /opt/rocm ]]; then LLAMA_BACKEND=rocm; else LLAMA_BACKEND=vulkan; fi ;;
    intel) LLAMA_BACKEND=vulkan ;;
  esac
  [[ $GPU_KIND == intel ]] && GPU_COUNT=1
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
