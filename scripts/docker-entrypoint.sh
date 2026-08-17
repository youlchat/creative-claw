#!/usr/bin/env sh
set -eu

DB="${CREATIVE_CLAW_DB:-/data/creative-claw.db}"
PROJECT_ROOT="${CREATIVE_CLAW_PROJECT_ROOT:-/data/projects/demo}"
PORT="${PORT:-8766}"

python /app/examples/bootstrap_demo.py --db "$DB" --project-root "$PROJECT_ROOT" --project-id demo
exec creative-claw --db "$DB" serve --host 0.0.0.0 --port "$PORT"
