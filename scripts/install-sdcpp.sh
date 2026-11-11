#!/usr/bin/env bash
# Optional image runtime installation; also called by install-suite.sh.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=$script_dir/../.venv/bin/python
[[ -x $python ]] || python=python3
exec "$python" "$script_dir/install-sdcpp.py" "$@"
