#!/usr/bin/env bash
# Optional Strata installation; the suite installer uses its own Python environment.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=$script_dir/../.venv/bin/python
[[ -x $python ]] || python=python3
exec "$python" "$script_dir/../engines/strata/install.py" "$@"
