#!/bin/sh
set -eu

APP_ROOT="/opt/kraken-trader"
APP_DIR="${APP_ROOT}/app"
APP_MAIN="${APP_DIR}/main.py"
APP_INIT="${APP_DIR}/__init__.py"
APP_UID=10001
APP_GID=10001
umask 077

# Home Assistant starts add-on containers as root by default. Root is used
# only to migrate legacy SQLite state and prepare narrowly scoped writable dirs.
# The mount root and Supervisor-managed options stay root-owned and non-writable
# by the trading process; Python itself always runs as the dedicated UID/GID.
if [ "$(id -u)" -eq 0 ]; then
  # HA's persistent /data mount may reject chmod even after a successful chown.
  # Create private child directories atomically under umask 077; never chmod mount paths.
  mkdir -p /data/state/private /data/logs/private /config/reports/tax/AT /run

  LEGACY_DB=/data/kraken_trader.sqlite3
  PREVIOUS_DB=/data/state/kraken_trader.sqlite3
  STATE_DB=/data/state/private/kraken_trader.sqlite3
  LEGACY_MIGRATION_MARKER=/data/state/.legacy_sqlite_migration
  PRIVATE_MIGRATION_MARKER=/data/state/.private_state_migration

  migrate_database() {
    source_db=$1
    target_db=$2
    marker=$3

    if [ -f "${source_db}" ] && [ ! -f "${marker}" ]; then
      if [ -e "${target_db}" ]; then
        echo "FATAL: both source and destination SQLite databases exist; manual reconciliation required." >&2
        exit 1
      fi
      for suffix in -wal -shm -journal; do
        if [ -e "${target_db}${suffix}" ]; then
          echo "FATAL: SQLite destination sidecar exists before migration; manual reconciliation required." >&2
          exit 1
        fi
      done
      : > "${marker}"
    fi

    if [ -f "${marker}" ]; then
      if [ -e "${source_db}" ] && [ -e "${target_db}" ]; then
        echo "FATAL: source and destination SQLite databases both exist during migration." >&2
        exit 1
      fi
      if [ -f "${source_db}" ]; then
        mv "${source_db}" "${target_db}"
      elif [ ! -f "${target_db}" ]; then
        echo "FATAL: SQLite migration marker exists but neither database is present." >&2
        exit 1
      fi
      for suffix in -wal -shm -journal; do
        if [ -e "${source_db}${suffix}" ]; then
          if [ -e "${target_db}${suffix}" ]; then
            echo "FATAL: SQLite sidecar collision during migration; manual reconciliation required." >&2
            exit 1
          fi
          mv "${source_db}${suffix}" "${target_db}${suffix}"
        fi
      done
      rm "${marker}"
    fi

    for suffix in -wal -shm -journal; do
      if [ -e "${source_db}${suffix}" ]; then
        echo "FATAL: orphaned SQLite sidecar exists at source path; manual reconciliation required." >&2
        exit 1
      fi
    done
  }

  # First complete any older root-level migration, then move the database from
  # v0.1.29/v0.1.30 into the private child directory without losing WAL sidecars.
  migrate_database "${LEGACY_DB}" "${PREVIOUS_DB}" "${LEGACY_MIGRATION_MARKER}"
  migrate_database "${PREVIOUS_DB}" "${STATE_DB}" "${PRIVATE_MIGRATION_MARKER}"

  chown -R "${APP_UID}:${APP_GID}" /data/state /data/logs /config/reports/tax
  chown 0:0 /data

  # Keep the Supervisor options file root-owned and unreadable to the app UID.
  # Pass only an ephemeral, owner-readable copy to the application process.
  if [ -f /data/options.json ]; then
    cp /data/options.json /run/kraken-trader-options.json
    chown "${APP_UID}:${APP_GID}" /run/kraken-trader-options.json
    chmod 0400 /run/kraken-trader-options.json
    export KRTR_OPTIONS_FILE=/run/kraken-trader-options.json
  fi
  export KRTR_STATE_DIR=/data/state/private
  export KRTR_LOG_DIR=/data/logs/private

  exec su-exec "${APP_UID}:${APP_GID}" "$0" "$@"
fi

STATE_DIR="${KRTR_STATE_DIR:-/data/state/private}"
LOG_DIR="${KRTR_LOG_DIR:-/data/logs/private}"
mkdir -p "$STATE_DIR" "$LOG_DIR"

if [ ! -w "$STATE_DIR" ] || [ ! -w "$LOG_DIR" ] || [ ! -w /config/reports/tax/AT ]; then
  echo "FATAL: non-root runtime cannot write its private state, AppLog, or tax export directory." >&2
  exit 1
fi
if [ -w /data ]; then
  echo "FATAL: non-root runtime must not have write access to the /data mount root." >&2
  exit 1
fi

echo "Kraken Trader container entrypoint: version=${APP_VERSION:-unknown} root=${APP_ROOT} cwd=$(pwd) python=$(command -v python || echo MISSING)" >&2

if [ ! -d "${APP_DIR}" ] || [ ! -f "${APP_MAIN}" ] || [ ! -f "${APP_INIT}" ]; then
  echo "FATAL: Kraken Trader Python package is missing." >&2
  echo "Expected: ${APP_MAIN} and ${APP_INIT}" >&2
  echo "Container working directory: $(pwd)" >&2
  echo "PYTHONPATH before correction: ${PYTHONPATH:-<unset>}" >&2
  echo "Available files under ${APP_ROOT}:" >&2
  ls -la "${APP_ROOT}" >&2 || true
  ls -la "${APP_DIR}" >&2 || true
  exit 1
fi

export PYTHONPATH="${APP_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${APP_ROOT}"

# Strip only the legacy direct-module command; always run the checked bootstrap.
if { [ "${1:-}" = "python" ] || [ "${1:-}" = "/usr/local/bin/python" ]; } &&
   [ "${2:-}" = "-m" ] && [ "${3:-}" = "app.main" ]; then
  shift 3
fi

echo "Kraken Trader startup wrapper: app_main=${APP_MAIN} PYTHONPATH=${PYTHONPATH}" >&2
exec python -c '
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import sys
import traceback

app_root = Path("/opt/kraken-trader")
app_dir = app_root / "app"
package_file = app_dir / "__init__.py"
main_file = app_dir / "main.py"

def safe_entries(path):
    try:
        return sorted(os.listdir(path))
    except OSError as exc:
        return [f"<unavailable:{type(exc).__name__}:errno={exc.errno}>"]


def safe_mode(path):
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError as exc:
        return f"<unavailable:{type(exc).__name__}:errno={exc.errno}>"


print(
    "Kraken Trader Python bootstrap: "
    f"executable={sys.executable} cwd={os.getcwd()} "
    f"uid={os.getuid()} gid={os.getgid()} "
    f"root_exists={app_root.is_dir()} app_dir_exists={app_dir.is_dir()} "
    f"app_dir_mode={safe_mode(app_dir)} "
    f"init_exists={package_file.is_file()} init_mode={safe_mode(package_file)} "
    f"main_exists={main_file.is_file()} main_mode={safe_mode(main_file)} "
    f"app_dir_entries={safe_entries(app_dir)}",
    file=sys.stderr,
    flush=True,
)

try:
    # Construct the top-level package directly from the known on-disk file path.
    # This intentionally bypasses top-level package discovery, which failed on HA
    # even though the wrapper and sys.path diagnostics showed the expected root.
    package_spec = spec_from_file_location(
        "app", str(package_file), submodule_search_locations=[str(app_dir)]
    )
    if package_spec is None or package_spec.loader is None:
        raise ImportError(f"Cannot construct application package spec from {package_file}")
    package_module = module_from_spec(package_spec)
    sys.modules["app"] = package_module
    package_spec.loader.exec_module(package_module)

    main_spec = spec_from_file_location("app.main", str(main_file))
    if main_spec is None or main_spec.loader is None:
        raise ImportError(f"Cannot construct app.main spec from {main_file}")
    main_module = module_from_spec(main_spec)
    sys.modules["app.main"] = main_module
    main_spec.loader.exec_module(main_module)
except Exception:
    traceback.print_exc(file=sys.stderr)
    print(
        "Kraken Trader bootstrap failure detail: "
        f"root_readable={os.access(app_root, os.R_OK)} "
        f"app_dir_readable={os.access(app_dir, os.R_OK)} "
        f"init_readable={os.access(package_file, os.R_OK)} "
        f"main_readable={os.access(main_file, os.R_OK)} "
        f"sys_path={sys.path!r}",
        file=sys.stderr,
        flush=True,
    )
    raise

sys.exit(main_module.main())
' "$@"
