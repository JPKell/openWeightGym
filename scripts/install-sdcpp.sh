#!/usr/bin/env bash
# Optional image runtime installation; also called by install-suite.sh.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/install-sdcpp.py" "$@"
