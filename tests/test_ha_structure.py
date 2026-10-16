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



def test_container_startup_has_explicit_module_path_and_build_smoke_test():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "kraken_margin_trader" / "Dockerfile").read_text(encoding="utf-8")
    run_script = (root / "kraken_margin_trader" / "run.sh").read_text(encoding="utf-8")

    assert "COPY app/ /opt/kraken-trader/app/" in dockerfile
    assert "chmod -R a+rX /opt/kraken-trader/app" in dockerfile
    assert "test -f /opt/kraken-trader/app/main.py" in dockerfile
    assert "import app.main" in dockerfile
    assert 'ENTRYPOINT ["/run.sh"]' in dockerfile
    assert "APP_VERSION=${BUILD_VERSION}" in dockerfile
    assert "APP_VERSION" in run_script
    assert "APP_MAIN=" in run_script and 'APP_MAIN="${APP_DIR}/main.py"' in run_script
    assert "PYTHONPATH=" in run_script
    assert "container entrypoint:" in run_script
    assert "spec_from_file_location" in run_script
    assert 'submodule_search_locations=[str(app_dir)]' in run_script
    assert 'spec_from_file_location("app.main", str(main_file))' in run_script
    assert "sys.exit(main_module.main())" in run_script
    assert "app_dir_entries=" in run_script
    assert "main_readable=" in run_script
    assert "def safe_entries(path):" in run_script
    assert "except OSError as exc:" in run_script

    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker run --rm --user 10001:10001 --tmpfs /data:rw,uid=10001,gid=10001,mode=700 kraken-trader-v2:ci --healthcheck" in workflow
    assert "docker run --rm --entrypoint /run.sh" not in workflow
    assert "&& /run.sh --healthcheck" in dockerfile


def test_apparmor_profile_grants_runtime_paths_without_disabling_confinement():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load(
        (root / "kraken_margin_trader" / "config.yaml").read_text(encoding="utf-8")
    )
    profile = (root / "kraken_margin_trader" / "apparmor.txt").read_text(encoding="utf-8")

    assert manifest["apparmor"] is True
    assert "profile kraken_margin_trader " in profile
    assert "/opt/kraken-trader/app/ r," in profile
    assert "/opt/kraken-trader/app/** r," in profile
    assert "/config/** rwk," in profile
    assert "/data/** rwk," in profile
    assert "#include <abstractions/nameservice>" in profile
    assert "network inet stream," in profile
    assert "network inet dgram," in profile
    assert "network inet6 stream," in profile
    assert "network inet6 dgram," in profile
    assert "file," not in profile
    assert "full_access: true" not in (root / "kraken_margin_trader" / "config.yaml").read_text(encoding="utf-8")

    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "apparmor_parser --skip-kernel-load --warn=all kraken_margin_trader/apparmor.txt" in workflow
