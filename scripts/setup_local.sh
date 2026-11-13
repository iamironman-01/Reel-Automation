#!/usr/bin/env bash
# One-time local setup (Linux / macOS / Git Bash). Run from anywhere:
#   bash scripts/setup_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required (found: $("$PY" --version 2>&1))."; exit 1; }

[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; else . .venv/Scripts/activate; fi

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python scripts/local_init.py

echo
echo "Setup done. Next:  bash scripts/run_local.sh demo"
