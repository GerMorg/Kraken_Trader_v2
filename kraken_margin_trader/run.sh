#!/bin/sh
set -eu

APP_ROOT="/opt/kraken-trader"
APP_MAIN="${APP_ROOT}/app/main.py"
APP_INIT="${APP_ROOT}/app/__init__.py"

# Give a clear diagnostic if an app image was built from the wrong context or
# a stale/incomplete image was installed instead of failing with a vague import error.
if [ ! -f "${APP_MAIN}" ] || [ ! -f "${APP_INIT}" ]; then
  echo "FATAL: Kraken Trader Python package is missing." >&2
  echo "Expected: ${APP_MAIN} and ${APP_INIT}" >&2
  echo "Container working directory: $(pwd)" >&2
  echo "Available files under ${APP_ROOT}:" >&2
  ls -la "${APP_ROOT}" >&2 || true
  ls -la "${APP_ROOT}/app" >&2 || true
  exit 1
fi

export PYTHONPATH="${APP_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${APP_ROOT}"

# Preserve arguments so CI and operators can run the non-trading health check.
exec python -m app.main "$@"
