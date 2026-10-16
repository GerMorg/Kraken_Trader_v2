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
    if (
        "container entrypoint:" not in run_script
        or "PYTHONPATH before correction" not in run_script
        or "APP_VERSION" not in run_script
    ):
        errors.append("run.sh must log image version and import-failure diagnostics")
    if "APP_VERSION=${BUILD_VERSION}" not in dockerfile:
        errors.append("Dockerfile must expose BUILD_VERSION for startup diagnostics")

    ci_path = root / ".github" / "workflows" / "ci.yml"
    ci_workflow = ci_path.read_text(encoding="utf-8")
    if "docker run --rm kraken-trader-v2:ci --healthcheck" not in ci_workflow:
        errors.append("CI must run the container using its declared default ENTRYPOINT")
    if "&& /run.sh --healthcheck" not in dockerfile:
        errors.append("Dockerfile build must execute the real startup bootstrap healthcheck")
    if "docker run --rm --entrypoint /run.sh" in ci_workflow:
        errors.append("CI must not bypass the image ENTRYPOINT during the container smoke test")

if not isinstance(repo, dict) or not repo.get("url"):
    errors.append("repository.yaml URL missing")

for required in (docker_path, run_path, package_path, pyproject_path, readme_path):
    if not required.is_file():
        errors.append(f"missing runtime file: {required.relative_to(root)}")

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
print("Home Assistant add-on structure and version validation passed")
