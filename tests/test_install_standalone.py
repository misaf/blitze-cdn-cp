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
    assert "--deploy" in result.stdout
    assert "--allow-empty-sites" in result.stdout
    assert "--public-address ADDRESS" in result.stdout


def test_standalone_installer_keeps_management_api_on_loopback():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "blitzecdn-api.service" in script
    assert "ssh -L 8000:127.0.0.1:8000" in script
    assert "--host 0.0.0.0" not in script


def test_standalone_installer_defaults_to_no_deployment_and_installs_cli_wrapper():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "run_deploy=0" in script
    assert "cli_wrapper=/usr/local/bin/blitzecdn" in script
    assert "Initial deployment skipped (safe default)" in script
    assert "API token (save it now)" not in script


def test_standalone_installer_guards_existing_sites_from_empty_state():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "managed_registry=/etc/nginx/blitzecdn-managed-sites" in script
    assert "this edge has managed sites but desired state is empty" in script
    assert "--deploy --allow-empty-sites" in script


def test_standalone_installer_does_not_take_ownership_of_role_managed_home_data():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "chown -R blitzecdn:blitzecdn /var/lib/blitzecdn" not in script
    assert "for service_path in .ansible .cache .ssh" in script
    assert "install -d -m 0751 -o blitzecdn -g blitzecdn /var/lib/blitzecdn" in script


def test_standalone_cli_wrapper_loads_the_persisted_environment():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "source /etc/blitzecdn/blitzecdn.env" in script
    assert "sudo -u blitzecdn bash -c" in script
    assert "awk '!/^BLITZE_ACME_DEFAULT_EMAIL=/'" in script
