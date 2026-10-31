#!/bin/sh
# Run from the extracted desktop download on Linux or macOS.
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    if python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))' 2>/dev/null; then
        python3 -m venv .venv
    else
        python3.13 -m venv .venv
    fi
fi
if [ ! -f .venv/voice-installed-aec-v1 ]; then
    .venv/bin/python -m pip install -r requirements.txt
    touch .venv/voice-installed-aec-v1
fi
exec .venv/bin/python -m voice.desktop "$@"
