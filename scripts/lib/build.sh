# shellcheck shell=bash
# Shared source-build toolchains. Packages remain installed for later builds and repairs.

# Toolkits are often installed outside PATH, including when installed earlier in this process.
gpu_build_tool() {  # gpu_build_tool nvcc|hipcc
  local tool=$1 candidate
  local candidates=("$(command -v "$tool" || true)")
  if [[ $tool == nvcc ]]; then
    candidates=("${CUDACXX:-}" "${CUDAToolkit_ROOT:-}/bin/nvcc" "${CUDA_PATH:-}/bin/nvcc" "${CUDA_HOME:-}/bin/nvcc"
                "${candidates[@]}" /opt/cuda/bin/nvcc /usr/local/cuda/bin/nvcc /usr/local/cuda-*/bin/nvcc)
  else
    candidates=("${ROCM_PATH:-}/bin/hipcc" "${HIP_PATH:-}/bin/hipcc" "${candidates[@]}" /opt/rocm/bin/hipcc)
  fi
  for candidate in "${candidates[@]}"; do
    if [[ -f $candidate && -x $candidate ]]; then printf '%s\n' "$candidate"; return 0; fi
  done
  return 1
}

cuda_build_ready() {
  local compiler root folder headers=0
  compiler=$(gpu_build_tool nvcc) || return 1
  root=$(dirname -- "$(dirname -- "$compiler")")
  for folder in "$root/include" "$root/targets/$OS_ARCH-linux/include"; do
    if [[ -r $folder/cuda_runtime.h && -r $folder/cublas_v2.h ]]; then headers=1; break; fi
  done
  ((headers)) || return 1
  for folder in "$root/lib64" "$root/lib" "$root/lib/$OS_ARCH-linux-gnu" "$root/targets/$OS_ARCH-linux/lib"; do
    [[ -r $folder/libcudart.so && -r $folder/libcublas.so ]] && return 0
  done
  return 1
}

# One missing group per line; development headers matter even when the runtime library is present.
missing_build_requirements() {
  local backend=$1 label=${2:-engine} tool missing=0 hip_root
  for tool in git cmake cc c++ make; do
    command -v "$tool" >/dev/null || missing=1
  done
  ((missing == 0)) || echo tools
  if ! command -v pkg-config >/dev/null || ! pkg-config --exists openssl; then echo openssl; fi
  if [[ $label == audio.cpp ]] && ! python3 -c "import ctypes; ctypes.CDLL('libespeak-ng.so.1')" >/dev/null 2>&1; then echo espeak; fi
  case "$backend" in
    cuda|cuda-*) cuda_build_ready || echo cuda ;;
    vulkan)
      if ! command -v glslc >/dev/null || ! command -v pkg-config >/dev/null ||
          ! pkg-config --exists vulkan SPIRV-Headers; then echo vulkan; fi ;;
    rocm)
      hip_root=$(gpu_build_tool hipcc) || hip_root=''
      [[ -z $hip_root ]] || hip_root=$(dirname -- "$(dirname -- "$hip_root")")
      if [[ -z $hip_root ]] || ! compgen -G "$hip_root/lib*/cmake/hipblas/hipblas-config.cmake" >/dev/null ||
          ! compgen -G "$hip_root/lib*/cmake/rocblas/rocblas-config.cmake" >/dev/null; then echo rocm; fi ;;
  esac
  return 0
}

install_build_requirements() {
  local backend=$1 label=${2:-llama.cpp} group names package
  local missing=() packages=() root=() hints=()
  mapfile -t missing < <(missing_build_requirements "$backend" "$label")
  ((${#missing[@]})) || return 0
  detect_package_manager
  ((${#PKG_INSTALL[@]})) || ui_die "Building $label needs: ${missing[*]}, and no supported package manager was found." \
    "Use a system with apt, dnf, zypper or pacman, then retry the installation."
  local -A seen=()
  for group in "${missing[@]}"; do
    [[ $group != cuda ]] || hints+=("CUDA package sources: https://docs.nvidia.com/cuda/cuda-installation-guide-linux/")
    names=$(build_dependency_packages "$group") || ui_die "No $group build packages are known for $PKG_MANAGER." \
      "Install the $group development packages for this system, then retry the installation."
    for package in $names; do
      [[ -z ${seen[$package]:-} ]] || continue
      packages+=("$package"); seen[$package]=1
    done
  done
  ui_info "Installing $label build requirements" "${packages[*]}"
  ui_sudo "installing $label build requirements (${packages[*]})"
  ((EUID == 0)) || root=(sudo -n)
  if [[ $PKG_MANAGER == apt ]]; then
    ui_step "Refreshing the package list" -- "${root[@]}" apt-get update ||
      ui_die "Could not refresh the package list for the $label build." "$(pkg_refresh_hint)"
  fi
  ui_step "Installing build requirements (${packages[*]})" -- "${root[@]}" "${PKG_INSTALL[@]}" "${packages[@]}" ||
    ui_die "Could not install $label's build requirements with $PKG_MANAGER." \
      "?$(pkg_refresh_hint)" "Check the packages: $(pkg_command "${packages[@]}")" \
      "${hints[@]}"
  hash -r
  mapfile -t missing < <(missing_build_requirements "$backend" "$label")
  ((${#missing[@]} == 0)) || ui_die "The packages were installed, but $label still needs: ${missing[*]}." \
    "Check the package installation and toolkit paths, then retry the installation."
}

