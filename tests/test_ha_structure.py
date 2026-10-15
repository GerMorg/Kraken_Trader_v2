from pathlib import Path
import subprocess
import sys

def test_addon_structure_validator_passes():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/"scripts"/"validate_ha_app.py")],
                          cwd=root,capture_output=True,text=True,check=False)
    assert result.returncode==0, result.stdout+result.stderr
