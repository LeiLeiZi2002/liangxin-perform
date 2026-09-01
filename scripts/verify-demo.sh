#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$ROOT/.venv/Scripts/python.exe"
[[ -x "$PYTHON" ]] || { echo "[backend] missing .venv Python" >&2; exit 1; }

echo "[backend] pytest"
(cd "$ROOT/backend" && "$PYTHON" -m pytest)
echo "[backend] ruff"
(cd "$ROOT/backend" && "$PYTHON" -m ruff check .)
echo "[backend] mypy"
MYPY_CACHE_DIR="$(mktemp -d)"
trap 'rm -rf "$MYPY_CACHE_DIR"' EXIT
(cd "$ROOT/backend" && "$PYTHON" -m mypy app --cache-dir "$MYPY_CACHE_DIR")
echo "[frontend] unit tests"
if [[ "$ROOT" == /mnt/* ]]; then
  (cd "$ROOT/frontend" && npm run test -- --run --pool=threads --maxWorkers=1)
else
  (cd "$ROOT/frontend" && npm run test -- --run)
fi
echo "[frontend] build"
(cd "$ROOT/frontend" && npm run build)
echo "[frontend] lint"
(cd "$ROOT/frontend" && npm run lint)
PLAYWRIGHT_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
if compgen -G "$PLAYWRIGHT_CACHE/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell" >/dev/null; then
  echo "[e2e] Playwright Chromium"
  (cd "$ROOT/frontend" && E2E_BROWSER_CHANNEL=chromium npm run e2e)
elif command -v google-chrome >/dev/null 2>&1; then
  echo "[e2e] system Chrome"
  (cd "$ROOT/frontend" && E2E_BROWSER_CHANNEL=chrome npm run e2e)
else
  echo "[e2e] skipped: WSL browser not installed; run npm run e2e from Windows PowerShell to use system Chrome."
fi
echo "All available checks passed."
