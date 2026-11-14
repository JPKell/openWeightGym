#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=${SUITE_PYTHON:-"$script_dir/../.venv/bin/python"}
[[ -x $python ]] || python=python3
exec "$python" "$script_dir/../engines/whisper_cpp/install.py" "$@"
