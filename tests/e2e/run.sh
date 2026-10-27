#!/usr/bin/env bash
# Start BackupBox in simulation mode on a free port, run the Playwright walk-through, stop it.
# Usage: tests/e2e/run.sh [screenshots-dir]
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
WORK=$(mktemp -d)
PORT=${PORT:-8765}
SHOTS=${1:-}

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT"
"$PYTHON" -m backupbox --simulate "$WORK/dev" --state-dir "$WORK/state" --buffer-dir "$WORK/pi-buffer" \
  --host 127.0.0.1 --port "$PORT" --poll 0.5 \
  >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break
  sleep 0.2
done

status=0
BASE_URL="http://127.0.0.1:$PORT" SIM_DIR="$WORK/dev" PYTHON="$PYTHON" SHOTS_DIR="$SHOTS" \
  node "$ROOT/tests/e2e/ui-flow.mjs" || status=$?
if [[ $status -ne 0 ]]; then
  echo "--- server log ---"
  tail -n 50 "$WORK/server.log"
fi
exit $status
