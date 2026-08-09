import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "install-standalone.sh"
BASH = "/bin/bash"


def test_standalone_installer_has_valid_shell_syntax():
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_standalone_installer_help_does_not_require_root():
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--admin-cidr CIDR" in result.stdout
    assert "--email ADDRESS" in result.stdout
    assert "--no-deploy" in result.stdout


def test_standalone_installer_keeps_management_api_on_loopback():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "blitzecdn-api.service" in script
    assert "ssh -L 8000:127.0.0.1:8000" in script
    assert "--host 0.0.0.0" not in script
