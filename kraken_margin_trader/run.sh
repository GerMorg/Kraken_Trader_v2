#!/bin/sh
set -eu

APP_ROOT="/opt/kraken-trader"
APP_DIR="${APP_ROOT}/app"
APP_MAIN="${APP_DIR}/main.py"
APP_INIT="${APP_DIR}/__init__.py"
APP_UID=10001
APP_GID=10001
umask 077

HEALTHCHECK_MODE=0
for arg in "$@"; do
  if [ "$arg" = "--healthcheck" ]; then
    HEALTHCHECK_MODE=1
  fi
done

# Home Assistant starts add-on containers as root by default. Root is used
# only to migrate legacy SQLite state and prepare narrowly scoped writable dirs.
# The mount root and Supervisor-managed options stay root-owned and non-writable
# by the trading process; Python itself always runs as the dedicated UID/GID.
if [ "$(id -u)" -eq 0 ]; then
  # Home Assistant can expose the persistent state/log mountpoints with mode 0700
  # owned by APP_UID. Do not create children inside them and never chmod a mount.
  mkdir -p /data/state /data/logs /run
  if [ "$HEALTHCHECK_MODE" -eq 0 ]; then
    mkdir -p /config/reports/tax/AT
  fi

  # Temporarily make the two existing mount directories traversable by this
  # root entrypoint without changing mode bits; ownership is restored before
  # the process drops privileges. No chmod is run on /data paths.
  chown 0:0 /data/state /data/logs

  LEGACY_DB=/data/kraken_trader.sqlite3
  STATE_DB=/data/state/kraken_trader.sqlite3
  MIGRATION_MARKER=/data/state/.legacy_sqlite_migration

  if [ -f "${LEGACY_DB}" ] && [ ! -f "${MIGRATION_MARKER}" ]; then
    if [ -e "${STATE_DB}" ]; then
      echo "FATAL: both legacy and state SQLite databases exist; manual reconciliation required." >&2
      exit 1
    fi
    for suffix in -wal -shm -journal; do
      if [ -e "${STATE_DB}${suffix}" ]; then
        echo "FATAL: destination SQLite sidecar exists before migration; manual reconciliation required." >&2
        exit 1
      fi
    done
    : > "${MIGRATION_MARKER}"
  fi

  if [ -f "${MIGRATION_MARKER}" ]; then
    if [ -e "${LEGACY_DB}" ] && [ -e "${STATE_DB}" ]; then
      echo "FATAL: legacy and state SQLite databases both exist during migration." >&2
      exit 1
    fi
    if [ -f "${LEGACY_DB}" ]; then
      mv "${LEGACY_DB}" "${STATE_DB}"
    elif [ ! -f "${STATE_DB}" ]; then
      echo "FATAL: SQLite migration marker exists but neither database is present." >&2
      exit 1
    fi
    for suffix in -wal -shm -journal; do
      if [ -e "${LEGACY_DB}${suffix}" ]; then
        if [ -e "${STATE_DB}${suffix}" ]; then
          echo "FATAL: SQLite sidecar collision during migration; manual reconciliation required." >&2
          exit 1
        fi
        mv "${LEGACY_DB}${suffix}" "${STATE_DB}${suffix}"
      fi
    done
    rm "${MIGRATION_MARKER}"
  fi

  for suffix in -wal -shm -journal; do
    if [ -e "${LEGACY_DB}${suffix}" ]; then
      echo "FATAL: orphaned legacy SQLite sidecar exists; manual reconciliation required." >&2
      exit 1
    fi
  done

  # Reassign files at the mount roots without traversing any historical nested
  # directory whose mode may deny root traversal. Keep the directories themselves
  # writable for APP_UID while preserving their existing mode bits.
  find /data/state -mindepth 1 -maxdepth 1 -type f -exec chown "${APP_UID}:${APP_GID}" {} +
  find /data/logs -mindepth 1 -maxdepth 1 -type f -exec chown "${APP_UID}:${APP_GID}" {} +
  chown "${APP_UID}:${APP_GID}" /data/state /data/logs
  if [ "$HEALTHCHECK_MODE" -eq 0 ]; then
    chown -R "${APP_UID}:${APP_GID}" /config/reports/tax
  fi

  # Supervisor-managed options stay root-owned; the app gets only a temporary
  # owner-readable copy. Keep this transient chmod away from persistent mounts.
  if [ -f /data/options.json ]; then
    cp /data/options.json /run/kraken-trader-options.json
    chmod 0400 /run/kraken-trader-options.json
    chown "${APP_UID}:${APP_GID}" /run/kraken-trader-options.json
    export KRTR_OPTIONS_FILE=/run/kraken-trader-options.json
  fi
  export KRTR_STATE_DIR=/data/state
  export KRTR_LOG_DIR=/data/logs

  exec su-exec "${APP_UID}:${APP_GID}" "$0" "$@"
fi

STATE_DIR="${KRTR_STATE_DIR:-/data/state}"
LOG_DIR="${KRTR_LOG_DIR:-/data/logs}"

if [ ! -d "$STATE_DIR" ] || [ ! -d "$LOG_DIR" ]; then
  echo "FATAL: non-root runtime requires existing mounted state and log directories." >&2
  exit 1
fi
if [ ! -w "$STATE_DIR" ] || [ ! -w "$LOG_DIR" ] || { [ "$HEALTHCHECK_MODE" -eq 0 ] && [ ! -w /config/reports/tax/AT ]; }; then
  echo "FATAL: non-root runtime cannot write its state, AppLog, or tax export directory." >&2
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
