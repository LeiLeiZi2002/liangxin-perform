#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/data/.runtime"
mkdir -p "$RUNTIME"

command -v node >/dev/null 2>&1 || { echo "Missing dependency: node. Run scripts/bootstrap-wsl.sh first." >&2; exit 1; }
[[ -d "$ROOT/.venv" ]] || { echo "Missing .venv. Create the Python environment and install backend dependencies first." >&2; exit 1; }
[[ -d "$ROOT/frontend/node_modules" ]] || { echo "Missing frontend/node_modules. Run npm install in frontend first." >&2; exit 1; }

PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$ROOT/.venv/Scripts/python.exe"
[[ -x "$PYTHON" ]] || { echo "Python executable was not found in .venv." >&2; exit 1; }
VITE="$ROOT/frontend/node_modules/.bin/vite"
[[ -x "$VITE" ]] || { echo "Vite executable was not found in frontend/node_modules." >&2; exit 1; }

cleanup() {
  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

cd "$ROOT/backend"
"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >"$RUNTIME/backend.log" 2>&1 &
BACKEND_PID=$!
cd "$ROOT/frontend"
"$VITE" --host 127.0.0.1 --port 5173 --strictPort >"$RUNTIME/frontend.log" 2>&1 &
FRONTEND_PID=$!
cd "$ROOT"
printf '%s\n' "$BACKEND_PID" >"$RUNTIME/backend.pid"
printf '%s\n' "$FRONTEND_PID" >"$RUNTIME/frontend.pid"

echo "Demo starting: http://127.0.0.1:5173"
echo "Logs: $RUNTIME/backend.log and $RUNTIME/frontend.log"
wait -n "$BACKEND_PID" "$FRONTEND_PID" || true
echo "A demo process exited. Check the logs above." >&2
exit 1
