#!/usr/bin/env bash
# Put llama.cpp's llama-server on this machine: the official ggml-org build that matches its
# graphics cards, or one built here from source. LoadCoach starts llama-server for each model it
# loads; this is where that program comes from. Also called by install.sh.
#
#   scripts/install-llamacpp.sh                     find the cards and install the matching build
#   scripts/install-llamacpp.sh --backend vulkan    or choose: cuda-13.4, cuda-12.8, rocm, vulkan, cpu
#   scripts/install-llamacpp.sh --from-source       build it here with cmake instead
#   scripts/install-llamacpp.sh --check             say what is installed and change nothing
#
# Builds are kept side by side in PREFIX/releases, and PREFIX/current points at the one in use, so
# going back is moving one link. Nothing is restarted: LoadCoach uses a new build for the models
# it loads from then on. The NVIDIA builds carry their own CUDA libraries; only the driver is needed.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
# shellcheck source=lib/ui.sh
source "$SCRIPT_DIR/lib/ui.sh"
# shellcheck source=lib/detect.sh
source "$SCRIPT_DIR/lib/detect.sh"
# This script as a person would type it, for the next steps it gives.
SELF=$(shell_path "$SCRIPT_DIR/install-llamacpp.sh")

BUILD=${OWG_LLAMACPP_BUILD:-b12008}  # llama.cpp 344846b4, the build qualified on 2026-10-30
PREFIX=${OWG_LLAMACPP_PREFIX:-$ROOT/runtimes/llamacpp}
RELEASES=https://github.com/ggml-org/llama.cpp/releases/download
SOURCE=https://github.com/ggml-org/llama.cpp.git
BACKEND=auto
FROM_SOURCE=0
CHECK=0
ASSUME_YES=0

usage() {
  cat <<EOF
Usage: scripts/install-llamacpp.sh [options]

  --backend NAME     cuda-13.4, cuda-12.8, rocm, vulkan or cpu (default: chosen from the cards)
  --build TAG        a llama.cpp release tag (default: $BUILD)
  --prefix DIR       where builds are kept (default: $PREFIX)
  --from-source      build llama-server here with cmake rather than downloading it
  --check            show the build in use and change nothing
  --yes              install missing system libraries without asking
  -h, --help         show this help

The program LoadCoach should run is then PREFIX/current/llama-server.
EOF
}

# A mistake in the options: usage_error "What is wrong"
usage_error() { OWG_STOP_TITLE='Nothing was changed' OWG_RETRY='' ui_die --status 2 "$1" "See every option and what it takes: $SELF --help"; }
while (($#)); do
  case "$1" in
    --backend|--build|--prefix) [[ -n ${2:-} ]] || usage_error "$1 needs a value." ;;&
    --backend) BACKEND=$2; shift 2 ;;
    --build) BUILD=$2; shift 2 ;;
    --prefix) PREFIX=$2; shift 2 ;;
    --from-source) FROM_SOURCE=1; shift ;;
    --check) CHECK=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "install-llamacpp.sh has no option $1." ;;
  esac
done
[[ $BACKEND =~ ^(auto|cuda-13\.4|cuda-12\.8|rocm|vulkan|cpu)$ ]] || usage_error "--backend is auto, cuda-13.4, cuda-12.8, rocm, vulkan or cpu, not $BACKEND."
[[ $BUILD =~ ^b[0-9]+$ ]] || usage_error "A build tag looks like b12008, not $BUILD."

# The release file for a backend on this processor.
asset_for() {
  local backend=$1 arch
  case "$OS_ARCH" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) return 1 ;; esac
  case "$backend:$arch" in
    cpu:*) echo "llama-$BUILD-bin-ubuntu-$arch.tar.gz" ;;
    vulkan:*) echo "llama-$BUILD-bin-ubuntu-vulkan-$arch.tar.gz" ;;
    cuda-13.4:*) echo "llama-$BUILD-bin-ubuntu-cuda-13.4-$arch.tar.gz" ;;
    cuda-12.8:x64) echo "llama-$BUILD-bin-ubuntu-cuda-12.8-x64.tar.gz" ;;
    rocm:x64) echo "llama-$BUILD-bin-ubuntu-rocm-10.0-x64.tar.gz" ;;
    *) return 1 ;;
  esac
}

# The libraries a build needs that this machine does not have, one per line.
missing_libraries() {
  local folder=$1
  LD_LIBRARY_PATH=$folder ldd "$folder/llama-server" "$folder"/libggml*.so* 2>/dev/null |
    awk '/=> not found/ {print $1}' | sort -u
}

# The C and C++ library versions a build needs that are newer than this system's: GLIBC_2.38 ...
# The prebuilt releases are made on a recent Ubuntu; older systems need a build made here.
missing_versions() {
  local folder=$1
  LD_LIBRARY_PATH=$folder ldd "$folder/llama-server" "$folder"/libggml*.so* 2>&1 |
    grep -oE "(GLIBC|GLIBCXX|CXXABI)_[0-9.]+'? not found" | grep -oE '(GLIBC|GLIBCXX|CXXABI)_[0-9.]+' | sort -uV
}

# What to do when the build cannot run here, for the ui_die that follows a failed supply_libraries.
LIBRARY_PROBLEM='' LIBRARY_STEPS=()
source_steps() {
  detect_package_manager
  LIBRARY_STEPS+=("Install the build tools: $(pkg_command "$(build_packages)")"
                  "Build llama.cpp on this machine: $SELF --from-source --backend $BACKEND")
}

# Offer to install the system packages for missing libraries. Returns 1 if some are still missing.
supply_libraries() {
  local folder=$1 library package have
  local -a missing packages=() unknown=() versions
  mapfile -t versions < <(missing_versions "$folder")
  if ((${#versions[@]})); then
    have=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$') || have=''
    LIBRARY_PROBLEM="This prebuilt llama.cpp needs ${versions[*]}, newer than this system's C library${have:+ ($have)}."
    source_steps
    return 1
  fi
  mapfile -t missing < <(missing_libraries "$folder")
  ((${#missing[@]})) || return 0
  detect_package_manager
  for library in "${missing[@]}"; do
    # The CUDA libraries come with the build itself; a missing libcuda means no NVIDIA driver.
    if [[ $library == libcuda.so* ]]; then
      LIBRARY_PROBLEM="The NVIDIA driver is not installed (its libcuda.so.1 is missing), so the CUDA build cannot run."
      LIBRARY_STEPS=("Install the NVIDIA driver for your distribution, then reboot" \
        "Or use the graphics card through Vulkan: $SELF --backend vulkan" \
        "Or run models on the processor: $SELF --backend cpu")
      return 1
    fi
    if package=$(package_for "$library"); then packages+=("$package"); else unknown+=("$library"); fi
  done
  if ((${#unknown[@]})); then
    LIBRARY_PROBLEM="llama-server needs libraries this installer does not know the package for: ${unknown[*]}"
    LIBRARY_STEPS=("Find the package that has them: ${PKG_MANAGER:-your package manager}'s search for ${unknown[0]}, then install it"
                   "Then run this again: $SELF")
    source_steps
    return 1
  fi
  mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort -u)
  ui_warn "llama-server needs: ${missing[*]}" "package: ${packages[*]}"
  if ((ASSUME_YES)) || ui_confirm "Install ${packages[*]} with $PKG_MANAGER?" y; then
    ui_sudo "installing ${packages[*]}"
    [[ $PKG_MANAGER == apt ]] && ui_step "Refreshing the package list" -- sudo -n apt-get update
    if ! ui_step "Installing ${packages[*]}" -- sudo -n "${PKG_INSTALL[@]}" "${packages[@]}"; then
      LIBRARY_PROBLEM="Could not install ${packages[*]}, which llama-server needs."
      LIBRARY_STEPS=("?$(pkg_refresh_hint)" "Install them yourself: $(pkg_command "${packages[@]}")" "Then run this again: $SELF")
      return 1
    fi
  fi
  mapfile -t missing < <(missing_libraries "$folder")
  if ((${#missing[@]})); then
    LIBRARY_PROBLEM="llama-server still cannot find: ${missing[*]}"
    LIBRARY_STEPS=("Install them yourself: $(pkg_command "${packages[@]}")" "Then run this again: $SELF")
    return 1
  fi
  return 0
}

download_release() {
  local backend=$1 target=$2 asset work
  asset=$(asset_for "$backend") || ui_die "There is no prebuilt $backend llama.cpp for this $OS_ARCH processor." \
    "Build it on this machine: $SELF --from-source --backend $backend" \
    "Or use another build: $SELF --backend cpu"
  work=$(mktemp -d)
  ui_download "$RELEASES/$BUILD/$asset" "$work/llama.tar.gz" "llama.cpp $BUILD $backend" ||
    ui_die "Could not download $asset from GitHub." \
      "Check this machine can reach GitHub: curl -sI https://github.com" \
      "Then run this again: $SELF --backend $backend"
  if [[ $backend == cuda-* ]]; then
    ui_download "$RELEASES/$BUILD/cudart-$asset" "$work/cudart.tar.gz" "CUDA runtime ${backend#cuda-}" ||
      ui_die "Could not download the CUDA runtime for $asset from GitHub." \
        "Check this machine can reach GitHub: curl -sI https://github.com" \
        "Then run this again: $SELF --backend $backend"
  fi
  rm -rf "$target.partial"
  mkdir -p "$target.partial"
  ui_step "Unpacking" -- bash -c 'for archive in "$1"/*.tar.gz; do tar -xzf "$archive" -C "$2" --strip-components=1; done' _ "$work" "$target.partial" ||
    ui_die "The llama.cpp download could not be unpacked into $(shell_path "$target.partial")." \
      "The lines above, from the log, say why: put that right" "Then run this again: $SELF --backend $backend"
  [[ -x $target.partial/llama-server ]] || ui_die "$asset holds no llama-server: the release may be packed differently now." \
    "Build it here instead: $SELF --from-source --backend $backend" "Or try another kind of build: $SELF --backend cpu"
  rm -rf "$target" "$work"
  mv "$target.partial" "$target"
}

build_from_source() {
  local backend=$1 target=$2 work flags=() jobs
  local tool
  for tool in git cmake c++ make; do
    command -v "$tool" >/dev/null || ui_die "Building llama.cpp needs $tool." "Install your distribution's build tools (git, cmake, a C++ compiler) and run this again."
  done
  case "$backend" in
    cuda-*) command -v nvcc >/dev/null || ui_die "A CUDA build needs nvcc, the CUDA toolkit." "Install it, or use the prebuilt build without --from-source."
            flags+=(-DGGML_CUDA=ON) ;;
    vulkan) flags+=(-DGGML_VULKAN=ON) ;;
    rocm) flags+=(-DGGML_HIP=ON) ;;
  esac
  work=$PREFIX/source/$BUILD
  if [[ ! -d $work/.git ]]; then
    mkdir -p "$(dirname "$work")"
    ui_step "Fetching llama.cpp $BUILD" -- git clone --quiet --depth 1 --branch "$BUILD" "$SOURCE" "$work"
  fi
  jobs=$(( $(nproc 2>/dev/null || echo 2) / 2 )); ((jobs > 0)) || jobs=1
  ui_note "Building with $jobs compilers at once. This takes a while; the log is $OWG_LOG"
  ui_step "Configuring the $backend build" -- cmake -S "$work" -B "$work/build-$backend" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF "${flags[@]}"
  ui_step "Compiling llama-server" -- cmake --build "$work/build-$backend" --target llama-server -j "$jobs"
  rm -rf "$target"
  mkdir -p "$target"
  cp -a "$work/build-$backend/bin/." "$target/"
}

main() {
  ui_init
  : "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/llamacpp-$(date +%Y%m%d-%H%M%S).log}"
  ui_log_init || ui_die "Cannot create a private engine installer log at $OWG_LOG." \
    "Check this account can write to $(shell_path "$(dirname "$OWG_LOG")"), or name another log: OWG_LOG=/tmp/owg-llamacpp.log $SELF"
  export OWG_LOG
  detect_os
  detect_gpu

  if ((CHECK)); then
    if [[ -x $PREFIX/current/llama-server ]]; then
      ui_ok "llama-server: $(readlink -f "$PREFIX/current")"
      "$PREFIX/current/llama-server" --version 2>&1 | grep -iE 'version|built' | sed 's/^/    /' || true
    else
      ui_warn "llama-server: not installed in $PREFIX"
    fi
    return 0
  fi

  [[ $BACKEND == auto ]] && BACKEND=$LLAMA_BACKEND
  local release=$BUILD-$BACKEND
  ((FROM_SOURCE)) && release+=-source
  local target=$PREFIX/releases/$release
  mkdir -p "$PREFIX/releases"

  if [[ -x $target/llama-server ]]; then
    ui_ok "llama.cpp $release is already here"
  elif ((FROM_SOURCE)); then
    build_from_source "$BACKEND" "$target"
  else
    download_release "$BACKEND" "$target"
  fi

  supply_libraries "$target" || ui_die "$LIBRARY_PROBLEM" "${LIBRARY_STEPS[@]}"
  local version
  if ! version=$("$target/llama-server" --version 2>&1); then
    ui__log "$version"
    [[ $release == *-source ]] || source_steps
    ui_die "llama-server $release did not start: $(printf '%s' "$version" | tail -1)" \
      "${LIBRARY_STEPS[@]}" "Or run models on the processor: $SELF --backend cpu"
  fi
  ui__log "$version"
  ui_ok "llama-server answers" "$(printf '%s' "$version" | grep -m1 -iE '^version' || true)"
  if [[ $BACKEND != cpu ]]; then
    local devices
    devices=$("$target/llama-server" --list-devices 2>&1 | sed -n '/Available devices/,$p' | tail -n +2 | grep -c . || true)
    if ((devices > 0)); then ui_ok "It sees $devices graphics device$( ((devices == 1)) || printf s)"
    else ui_warn "It lists no graphics devices" "models will run on the CPU until the driver is working"; fi
  fi
  ln -sfn "releases/$release" "$PREFIX/current"
  ui_ok "In use: $PREFIX/current/llama-server"
}

main "$@"
