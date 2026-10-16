from pathlib import Path
import subprocess
import sys

import yaml


def test_addon_structure_validator_passes():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "validate_ha_app.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_readme_release_version_matches_addon_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load(
        (root / "kraken_margin_trader" / "config.yaml").read_text(encoding="utf-8")
    )
    readme = (root / "README.md").read_text(encoding="utf-8")
    status_lines = [
        line.strip()
        for line in readme.splitlines()
        if line.strip().startswith("## Aktueller Status")
    ]
    assert status_lines == [f"## Aktueller Status — {manifest['version']}"]



def test_manifest_uses_writable_app_config_and_docker_supervisor_labels():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load(
        (root / "kraken_margin_trader" / "config.yaml").read_text(encoding="utf-8")
    )
    dockerfile = (root / "kraken_margin_trader" / "Dockerfile").read_text(encoding="utf-8")

    app_config_maps = [
        item for item in manifest.get("map", [])
        if isinstance(item, dict) and item.get("type") == "app_config"
    ]
    assert len(app_config_maps) == 1
    assert app_config_maps[0].get("read_only") is False
    assert f"ARG BUILD_VERSION={manifest['version']}" in dockerfile
    assert 'io.hass.version="${BUILD_VERSION}"' in dockerfile
    assert 'io.hass.type="app"' in dockerfile
    assert 'io.hass.arch="${BUILD_ARCH}"' in dockerfile
