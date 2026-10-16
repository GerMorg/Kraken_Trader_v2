#!/bin/sh
set -eu

APP_ROOT="/opt/kraken-trader"
APP_MAIN="${APP_ROOT}/app/main.py"
APP_INIT="${APP_ROOT}/app/__init__.py"

echo "Kraken Trader container entrypoint: version=${APP_VERSION:-unknown} root=${APP_ROOT} cwd=$(pwd) python=$(command -v python || echo MISSING)" >&2

if [ ! -f "${APP_MAIN}" ] || [ ! -f "${APP_INIT}" ]; then
  echo "FATAL: Kraken Trader Python package is missing." >&2
  echo "Expected: ${APP_MAIN} and ${APP_INIT}" >&2
  echo "Container working directory: $(pwd)" >&2
  echo "PYTHONPATH before correction: ${PYTHONPATH:-<unset>}" >&2
  echo "Available files under ${APP_ROOT}:" >&2
  ls -la "${APP_ROOT}" >&2 || true
  ls -la "${APP_ROOT}/app" >&2 || true
  exit 1
fi

export PYTHONPATH="${APP_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${APP_ROOT}"

# Home Assistant/Supervisor may preserve a legacy container command. Strip
# only that exact prefix, then launch through a Python bootstrap that explicitly
# inserts APP_ROOT into sys.path before importing the package.
if { [ "${1:-}" = "python" ] || [ "${1:-}" = "/usr/local/bin/python" ]; } &&
   [ "${2:-}" = "-m" ] && [ "${3:-}" = "app.main" ]; then
  shift 3
fi

echo "Kraken Trader startup wrapper: app_main=${APP_MAIN} PYTHONPATH=${PYTHONPATH}" >&2
exec python -c '
import os
import sys

app_root = "/opt/kraken-trader"
sys.path.insert(0, app_root)
print(
    f"Kraken Trader Python bootstrap: executable={sys.executable} "
    f"cwd={os.getcwd()} sys.path[0]={sys.path[0]}",
    file=sys.stderr,
    flush=True,
)
import app.main
sys.exit(app.main.main())
' "$@"
