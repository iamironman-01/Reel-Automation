#!/usr/bin/env bash
# Run ReelDrop locally (Linux / macOS / Git Bash).
#   bash scripts/run_local.sh demo        dry-run demo in an isolated folder (nothing is sent)
#   bash scripts/run_local.sh scheduler   the real scheduler, using your .env and config
#   bash scripts/run_local.sh dashboard   the web dashboard at http://localhost:8501
#   bash scripts/run_local.sh test        run the test suite
#   bash scripts/run_local.sh cli ...     queue commands, e.g.  cli add "<reel url>"  /  cli list
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then . .venv/bin/activate
elif [ -f .venv/Scripts/activate ]; then . .venv/Scripts/activate
else echo "Run  bash scripts/setup_local.sh  first."; exit 1; fi

cmd="${1:-}"; shift || true
case "$cmd" in
  demo)      exec python scripts/local_demo.py "$@" ;;
  scheduler) exec python main.py ;;
  dashboard) exec python -m streamlit run dashboard.py ;;
  test)      exec python -m pytest -q "$@" ;;
  cli)       exec python -m reeldrop "$@" ;;
  *) sed -n '2,7p' "$0"; exit 1 ;;
esac
