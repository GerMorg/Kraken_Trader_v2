from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import tomllib

try:
    import yaml
except ImportError:
    print("PyYAML is required for Home Assistant manifest validation", file=sys.stderr)
    raise

root = Path(__file__).resolve().parents[1]
manifest_path = root / "kraken_margin_trader" / "config.yaml"
repo_path = root / "repository.yaml"
docker_path = root / "kraken_margin_trader" / "Dockerfile"
run_path = root / "kraken_margin_trader" / "run.sh"
apparmor_path = root / "kraken_margin_trader" / "apparmor.txt"
package_path = root / "kraken_margin_trader" / "app" / "__init__.py"
pyproject_path = root / "pyproject.toml"
readme_path = root / "README.md"
errors: list[str] = []

try:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    repo = yaml.safe_load(repo_path.read_text(encoding="utf-8"))
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    package_tree = ast.parse(package_path.read_text(encoding="utf-8"))
    readme = readme_path.read_text(encoding="utf-8")
    dockerfile = docker_path.read_text(encoding="utf-8")
    run_script = run_path.read_text(encoding="utf-8")
    apparmor_profile = apparmor_path.read_text(encoding="utf-8")
except (OSError, yaml.YAMLError, tomllib.TOMLDecodeError, SyntaxError) as exc:
    print(f"manifest or package metadata parse failed: {type(exc).__name__}", file=sys.stderr)
    raise SystemExit(1)

package_version = None
for node in package_tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__version__":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    package_version = node.value.value

project_version = project.get("project", {}).get("version")

if not isinstance(manifest, dict):
    errors.append("config.yaml must contain a mapping")
else:
    for key in ("name", "version", "slug", "description", "arch", "startup", "boot", "init", "options", "schema"):
        if key not in manifest:
            errors.append(f"missing required add-on key: {key}")
    if manifest.get("slug") != "kraken_margin_trader":
        errors.append("unexpected add-on slug")
    manifest_version = str(manifest.get("version", ""))
    if not manifest_version:
        errors.append("manifest version missing")
    if project_version != manifest_version:
        errors.append("manifest version does not match pyproject project.version")
    if package_version != manifest_version:
        errors.append("manifest version does not match app.__version__")
    # The README version is user-facing release metadata. Keep it under the same
    # CI invariant as the package, project metadata, and Home Assistant manifest.
    status_lines = [
        line.strip()
        for line in readme.splitlines()
        if line.strip().startswith("## Aktueller Status")
    ]
    expected_status = f"## Aktueller Status — {manifest_version}"
    if status_lines != [expected_status]:
        errors.append("README current-status version does not match add-on manifest version")
    if "sensors_enabled" in manifest.get("options", {}):
        errors.append("sensors must not be a product option")
    if manifest.get("apparmor") is not True:
        errors.append("AppArmor must remain enabled for the trading app")

    mappings = manifest.get("map", [])
    if not isinstance(mappings, list):
        errors.append("manifest map must be a list")
    else:
        app_config_maps = [
            item for item in mappings
            if isinstance(item, dict) and item.get("type") == "app_config"
        ]
        if len(app_config_maps) != 1:
            errors.append("exactly one app_config mapping is required for user-visible tax exports")
        elif app_config_maps[0].get("read_only", True) is not False:
            errors.append("app_config mapping must be writable for tax report exports")

    required_docker_labels = (
        'io.hass.version="${BUILD_VERSION}"',
        'io.hass.type="app"',
        'io.hass.arch="${BUILD_ARCH}"',
    )
    for label in required_docker_labels:
        if label not in dockerfile:
            errors.append(f"Dockerfile missing Home Assistant app label: {label}")

    build_version_match = re.search(r"(?m)^ARG BUILD_VERSION=(\S+)\s*$", dockerfile)
    if not build_version_match or build_version_match.group(1) != manifest_version:
        errors.append("Dockerfile BUILD_VERSION default must match config.yaml version")
    if not re.search(r"(?m)^ARG BUILD_ARCH=\S+\s*$", dockerfile):
        errors.append("Dockerfile BUILD_ARCH argument is required")

    if "COPY app/ /opt/kraken-trader/app/" not in dockerfile:
        errors.append("Dockerfile must copy the Python package into the configured app root")
    if "chmod -R a+rX /opt/kraken-trader/app" not in dockerfile:
        errors.append("Dockerfile must make app source readable and package directories searchable for runtime UIDs")
    if "import app.main" not in dockerfile:
        errors.append("Dockerfile must smoke-test importing app.main during image build")
    if 'ENTRYPOINT ["/run.sh"]' not in dockerfile:
        errors.append("Dockerfile must enforce run.sh as the container ENTRYPOINT")
    if "APP_MAIN=" not in run_script or 'APP_MAIN="${APP_DIR}/main.py"' not in run_script:
        errors.append("run.sh must validate the expected app.main path before startup")
    if "PYTHONPATH=" not in run_script:
        errors.append("run.sh must explicitly configure the Python package search path")
    if "spec_from_file_location" not in run_script:
        errors.append("run.sh must load the application package from explicit file paths")
    if 'submodule_search_locations=[str(app_dir)]' not in run_script:
        errors.append("run.sh must set the application package search location explicitly")
    if 'spec_from_file_location("app.main", str(main_file))' not in run_script:
        errors.append("run.sh must load app.main from its explicit source path")
    if "sys.exit(main_module.main())" not in run_script:
        errors.append("run.sh must execute the loaded app.main module")
    if "app_dir_entries=" not in run_script or "main_readable=" not in run_script:
        errors.append("run.sh must emit package filesystem diagnostics on startup/failure")
    if "def safe_entries(path):" not in run_script or "except OSError as exc:" not in run_script:
        errors.append("run.sh diagnostics must tolerate inaccessible directories without masking the root error")
    if (
        "container entrypoint:" not in run_script
        or "PYTHONPATH before correction" not in run_script
        or "APP_VERSION" not in run_script
    ):
        errors.append("run.sh must log image version and import-failure diagnostics")
    if "APP_VERSION=${BUILD_VERSION}" not in dockerfile:
        errors.append("Dockerfile must expose BUILD_VERSION for startup diagnostics")
    if "mkdir -p /data/state /data/logs" not in dockerfile:
        errors.append("Dockerfile must create dedicated state and log directories")
    if "STATE_DB=/data/state/private/kraken_trader.sqlite3" not in run_script or "LEGACY_MIGRATION_MARKER=/data/state/.legacy_sqlite_migration" not in run_script or "PRIVATE_MIGRATION_MARKER=/data/state/.private_state_migration" not in run_script:
        errors.append("entrypoint must migrate legacy SQLite state into the private directory before dropping root")
    if "chown 0:0 /data" not in run_script or "chmod 0755 /data" in run_script:
        errors.append("data mount root must remain root-owned without chmod on the HA mount")
    if "umask 077" not in run_script or "LEGACY_DB=/data/kraken_trader.sqlite3" not in run_script or "PREVIOUS_DB=/data/state/kraken_trader.sqlite3" not in run_script:
        errors.append("SQLite state must be migrated and private child directories created under a restrictive umask")
    if any(line.strip().startswith("chmod ") and "/data" in line for line in run_script.splitlines()):
        errors.append("entrypoint must not invoke chmod on mounted /data paths")
    if 'export KRTR_STATE_DIR=/data/state/private' not in run_script or 'export KRTR_LOG_DIR=/data/logs/private' not in run_script:
        errors.append("entrypoint must pass private state and log paths to the runtime")
    if '[ ! -w "$STATE_DIR" ]' not in run_script or "[ ! -w /config/reports/tax/AT ]" not in run_script or "[ -w /data ]" not in run_script:
        errors.append("entrypoint must verify writable private paths and a non-writable data mount root")
    if "apk add --no-cache su-exec" not in dockerfile or 'exec su-exec "${APP_UID}:${APP_GID}" "$0" "$@"' not in run_script:
        errors.append("default container startup must drop root privileges with su-exec")
    if 'if [ "$(id -u)" -eq 0 ]; then' not in run_script:
        errors.append("run.sh must prepare writable mounts before dropping root")
    if "KRTR_OPTIONS_FILE=/run/kraken-trader-options.json" not in run_script:
        errors.append("runtime must pass a private options copy to the unprivileged process")
    # A custom AppArmor profile is mandatory because application code, DNS sockets,
    # Python runtime libraries, privilege dropping, and the writable tax-report mount need explicit access.
    if "profile kraken_margin_trader " not in apparmor_profile:
        errors.append("AppArmor profile name must match the Home Assistant add-on profile")
    for permission in (
        "/opt/kraken-trader/app/ r,",
        "/opt/kraken-trader/app/** r,",
        "/config/** rwk,",
        "/data/ rw,",
        "/data/state/ rwk,",
        "/data/state/private/ rwk,",
        "/data/logs/ rwk,",
        "/data/logs/private/ rwk,",
        "/data/** rwk,",
        "network inet stream,",
        "network inet dgram,",
        "network inet6 stream,",
        "network inet6 dgram,",
        "#include <abstractions/nameservice>",
        "/sbin/su-exec ix,",
        "capability chown,",
        "capability setgid,",
        "capability setuid,",
    ):
        if permission not in apparmor_profile:
            errors.append(f"AppArmor profile missing required permission: {permission}")
    if re.search(r"(?m)^\s*file\s*,\s*$", apparmor_profile):
        errors.append("AppArmor profile must not use a blanket file-access grant")

    ci_path = root / ".github" / "workflows" / "ci.yml"
    ci_workflow = ci_path.read_text(encoding="utf-8")
    if "docker run --rm --user 10001:10001 --tmpfs /data/state:rw,uid=10001,gid=10001,mode=700 --tmpfs /data/logs:rw,uid=10001,gid=10001,mode=700 --tmpfs /config:rw,uid=10001,gid=10001,mode=700 kraken-trader-v2:ci --healthcheck" not in ci_workflow:
        errors.append("CI must run the entrypoint directly as an unprivileged user with writable temporary data")
    if "docker run --rm --mount type=bind,source=" not in ci_workflow or "target=/data --tmpfs /config:rw,uid=0,gid=0,mode=755 kraken-trader-v2:ci --healthcheck 2>&1" not in ci_workflow:
        errors.append("CI must exercise privilege dropping with root-owned writable mounts")
    if 'grep -F "uid=10001 gid=10001"' not in ci_workflow:
        errors.append("CI must assert that Python starts as the dedicated unprivileged UID")
    if 'grep -F \'"credentials_present":true\'' not in ci_workflow:
        errors.append("CI must verify the private options copy is parsed without exposing secret values")
    if "legacy_probe" not in ci_workflow or "preserved" not in ci_workflow or "0:0:400" not in ci_workflow or "state/private/kraken_trader.sqlite3" not in ci_workflow or "state/private" not in ci_workflow or "logs/private" not in ci_workflow:
        errors.append("CI must verify legacy SQLite migration preserves data and protects options.json")
    if "SIMULATED_HA_MOUNT_CHMOD_DENIAL" not in ci_workflow or "deny-mounted-chmod" not in ci_workflow:
        errors.append("CI must simulate EPERM for chmod on mounted /data paths")
    if "&& /run.sh --healthcheck" not in dockerfile:
        errors.append("Dockerfile build must execute the real startup bootstrap healthcheck")
    if "docker run --rm --entrypoint /run.sh" in ci_workflow:
        errors.append("CI must not bypass the image ENTRYPOINT during the container smoke test")

if not isinstance(repo, dict) or not repo.get("url"):
    errors.append("repository.yaml URL missing")

for required in (docker_path, run_path, apparmor_path, package_path, pyproject_path, readme_path):
    if not required.is_file():
        errors.append(f"missing runtime file: {required.relative_to(root)}")

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
print("Home Assistant add-on structure and version validation passed")
