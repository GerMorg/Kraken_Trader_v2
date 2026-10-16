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
