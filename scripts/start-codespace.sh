#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/data/.runtime"
mkdir -p "$RUNTIME"

[[ -d "$ROOT/.venv" ]] || {
  echo "Missing .venv. Rebuild the container or run the post-create setup first." >&2
  exit 1
}
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || {
  echo "Python executable was not found in .venv." >&2
  exit 1
}
[[ -d "$ROOT/frontend/node_modules" ]] || {
  echo "Missing frontend/node_modules. Rebuild the container or run npm ci first." >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo "Missing dependency: npm." >&2
  exit 1
}

CODESPACES_SHARED_ENV="${CODESPACES_SHARED_ENV:-/workspaces/.codespaces/shared/.env}"

read_codespaces_value() {
  local name="$1"
  [[ -r "$CODESPACES_SHARED_ENV" ]] || return 1
  sed -n "s/^${name}=//p" "$CODESPACES_SHARED_ENV" | tail -n 1 | tr -d '\r'
}

if [[ -z "${CODESPACE_NAME:-}" ]]; then
  CODESPACE_NAME="$(read_codespaces_value CODESPACE_NAME || true)"
fi
if [[ -z "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]]; then
  GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="$(read_codespaces_value GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN || true)"
fi

: "${CODESPACE_NAME:?CODESPACE_NAME is required in GitHub Codespaces}"
: "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:?GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN is required in GitHub Codespaces}"
CODESPACES_FRONTEND_HOST="${CODESPACE_NAME}-5173.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
export FRONTEND_ORIGIN="https://${CODESPACES_FRONTEND_HOST}"
export __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS="${CODESPACES_FRONTEND_HOST}"
export VITE_API_BASE_URL=""

BACKEND_PID_FILE="$RUNTIME/backend.pid"
FRONTEND_PID_FILE="$RUNTIME/frontend.pid"

project_pid() {
  local pid_file="$1"
  local expected_cwd="$2"
  local expected_command="$3"
  local pid cwd command_line

  [[ -s "$pid_file" ]] || return 1
  pid="$(<"$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -e "/proc/$pid/cwd" && -e "/proc/$pid/cmdline" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$expected_cwd" ]] || return 1
  command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$command_line" == *"$expected_command"* ]] || return 1
  printf '%s\n' "$pid"
}

wait_for_project_pid() {
  local pid_file="$1"
  local expected_cwd="$2"
  local expected_command="$3"
  local deadline=$((SECONDS + 10))

  while (( SECONDS < deadline )); do
    if project_pid "$pid_file" "$expected_cwd" "$expected_command" >/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

BACKEND_PID=""
FRONTEND_PID=""
if BACKEND_PID="$(project_pid "$BACKEND_PID_FILE" "$ROOT/backend" 'app.main:app')"; then
  echo "Reusing running backend process $BACKEND_PID."
fi
if FRONTEND_PID="$(project_pid "$FRONTEND_PID_FILE" "$ROOT/frontend" 'npm run dev')"; then
  echo "Reusing running frontend process $FRONTEND_PID."
fi

exec 9>"$RUNTIME/start.lock"
flock -n 9 || {
  echo "Another Codespaces startup is already in progress; exiting." >&2
  exit 0
}

if [[ -n "$BACKEND_PID" && -n "$FRONTEND_PID" ]]; then
  echo "Codespaces services are already running."
  exit 0
fi

if [[ -z "$BACKEND_PID" ]]; then
  (
    cd "$ROOT/backend"
    exec "$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  ) 9>&- >"$RUNTIME/backend.log" 2>&1 &
  BACKEND_PID=$!
  printf '%s\n' "$BACKEND_PID" >"$BACKEND_PID_FILE"
fi

if [[ -z "$FRONTEND_PID" ]]; then
  (
    cd "$ROOT/frontend"
    exec npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
  ) 9>&- >"$RUNTIME/frontend.log" 2>&1 &
  FRONTEND_PID=$!
  printf '%s\n' "$FRONTEND_PID" >"$FRONTEND_PID_FILE"
fi

cleanup() {
  trap - EXIT INT TERM
  for pid_spec in \
    "$BACKEND_PID|$BACKEND_PID_FILE|$ROOT/backend|app.main:app" \
    "$FRONTEND_PID|$FRONTEND_PID_FILE|$ROOT/frontend|npm run dev"; do
    IFS='|' read -r pid pid_file expected_cwd expected_command <<<"$pid_spec"
    [[ -n "$pid" ]] || continue
    if project_pid "$pid_file" "$expected_cwd" "$expected_command" >/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"
}
trap cleanup EXIT INT TERM

if ! wait_for_project_pid "$BACKEND_PID_FILE" "$ROOT/backend" 'app.main:app'; then
  echo "Backend process did not become ready." >&2
  exit 1
fi
if ! wait_for_project_pid "$FRONTEND_PID_FILE" "$ROOT/frontend" 'npm run dev'; then
  echo "Frontend process did not become ready." >&2
  exit 1
fi

echo "Codespaces services starting on https://${CODESPACES_FRONTEND_HOST}."
echo "Logs: $RUNTIME/backend.log and $RUNTIME/frontend.log"

while :; do
  if ! project_pid "$BACKEND_PID_FILE" "$ROOT/backend" 'app.main:app' >/dev/null; then
    echo "Backend process exited; stopping frontend." >&2
    exit 1
  fi
  if ! project_pid "$FRONTEND_PID_FILE" "$ROOT/frontend" 'npm run dev' >/dev/null; then
    echo "Frontend process exited; stopping backend." >&2
    exit 1
  fi
  sleep 2
done
