#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DATABASE="${CREATIVE_CLAW_DB:-.creative-claw/demo.db}"
PROJECT_ROOT="${CREATIVE_CLAW_PROJECT_ROOT:-.creative-claw/projects/demo}"
PORT="${PORT:-8766}"
HOST="${HOST:-127.0.0.1}"

if [ ! -x .venv/bin/python ]; then
  printf '%s\n' '[1/4] Creating Python virtual environment .venv'
  "$PYTHON_BIN" -m venv .venv
fi
printf '%s\n' '[2/4] Installing Creative Claw and dependencies'
.venv/bin/python -m pip install --disable-pip-version-check -e .
printf '%s\n' '[3/4] Bootstrapping the idempotent demo project'
.venv/bin/python examples/bootstrap_demo.py --db "$DATABASE" --project-root "$PROJECT_ROOT" --project-id demo

URL="http://127.0.0.1:$PORT/"
if [ "${NO_BROWSER:-0}" != "1" ]; then
  if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  fi
fi
printf '%s\n' "[4/4] Creative Claw is running at $URL"
printf '%s\n' 'Press Ctrl+C to stop. Configure an optional model in the web UI.'
exec .venv/bin/python -m creative_claw --db "$DATABASE" serve --host "$HOST" --port "$PORT"
