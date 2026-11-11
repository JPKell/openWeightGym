#!/usr/bin/env bash
# Prepare a source-build toolchain; stdout is the CUDA compiler path, diagnostics go to stderr.
# This installs system packages permanently. It never removes packages or builds a model engine.
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib/ui.sh"
source "$SCRIPT_DIR/lib/detect.sh"
source "$SCRIPT_DIR/lib/build.sh"
backend=${1:-} label=${2:-engine}
[[ $# == 2 && $backend =~ ^(cuda|vulkan|cpu|rocm|cuda-13\.4|cuda-12\.8)$ ]] || {
  echo 'Usage: install-build-tools.sh BACKEND LABEL' >&2; exit 2;
}
{
  ui_init
  : "${OWG_LOG:=${XDG_STATE_HOME:-$HOME/.local/state}/openweightgym/build-tools-$(date +%Y%m%d-%H%M%S).log}"
  ui_log_init || ui_die "Cannot write the build-tools log: $OWG_LOG" 'Choose a writable OWG_LOG path and retry'
  export OWG_LOG
  detect_os
  install_build_requirements "$backend" "$label"
} >&2
case "$backend" in cuda|cuda-*) gpu_build_tool nvcc ;; esac
exit 0
