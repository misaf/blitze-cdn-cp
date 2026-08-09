import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "install.sh"
BASH = "/bin/bash"


def test_installer_has_valid_shell_syntax():
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_installer_preserves_and_rebuilds_an_incomplete_virtualenv():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "! -x .venv/bin/python || ! -x .venv/bin/pip" in script
    assert ".venv.invalid.$(date -u +%Y%m%dT%H%M%SZ)" in script
    assert ".venv/bin/python -m pip install" in script
