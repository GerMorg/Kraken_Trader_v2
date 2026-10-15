from __future__ import annotations

from pathlib import Path
import sys

try:
    import yaml
except ImportError:
    print("PyYAML is required for Home Assistant manifest validation", file=sys.stderr)
    raise

root=Path(__file__).resolve().parents[1]
manifest_path=root/"kraken_margin_trader"/"config.yaml"
repo_path=root/"repository.yaml"
docker_path=root/"kraken_margin_trader"/"Dockerfile"
run_path=root/"kraken_margin_trader"/"run.sh"
package_path=root/"kraken_margin_trader"/"app"/"__init__.py"
errors=[]
try:
    manifest=yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    repo=yaml.safe_load(repo_path.read_text(encoding="utf-8"))
except (OSError,yaml.YAMLError) as exc:
    print(f"manifest parse failed: {type(exc).__name__}",file=sys.stderr)
    raise SystemExit(1)
if not isinstance(manifest,dict):
    errors.append("config.yaml must contain a mapping")
else:
    for key in ("name","version","slug","description","arch","startup","boot","init","options","schema"):
        if key not in manifest:
            errors.append(f"missing required add-on key: {key}")
    if manifest.get("slug")!="kraken_margin_trader":
        errors.append("unexpected add-on slug")
    if manifest.get("version")!="0.1.0":
        errors.append("manifest version must match initial package version")
    if "sensors_enabled" in manifest.get("options",{}):
        errors.append("sensors must not be a product option")
if not isinstance(repo,dict) or not repo.get("url"):
    errors.append("repository.yaml URL missing")
for required in (docker_path,run_path,package_path):
    if not required.is_file():
        errors.append(f"missing runtime file: {required.relative_to(root)}")
if errors:
    for error in errors: print(f"ERROR: {error}",file=sys.stderr)
    raise SystemExit(1)
print("Home Assistant add-on structure validation passed")
