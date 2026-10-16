from __future__ import annotations

import ast
from pathlib import Path
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
