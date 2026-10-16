#!/bin/sh
set -eu

APP_ROOT="/opt/kraken-trader"
APP_MAIN="${APP_ROOT}/app/main.py"
APP_INIT="${APP_ROOT}/app/__init__.py"

echo "Kraken Trader container entrypoint: version=${APP_VERSION:-unknown} root=${APP_ROOT} cwd=$(pwd) python=$(command -v python || echo MISSING)" >&2

# Give a clear diagnostic if an app image was built from the wrong context or
# a stale/incomplete image was installed instead of failing with a vague import error.
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

# Strip only the legacy command previously used to start this module directly.
# The checked wrapper is always responsible for importing and starting the app.
if { [ "${1:-}" = "python" ] || [ "${1:-}" = "/usr/local/bin/python" ]; } &&
   [ "${2:-}" = "-m" ] && [ "${3:-}" = "app.main" ]; then
  shift 3
fi

echo "Kraken Trader startup wrapper: app_main=${APP_MAIN} PYTHONPATH=${PYTHONPATH}" >&2
exec python -m app.main "$@"
