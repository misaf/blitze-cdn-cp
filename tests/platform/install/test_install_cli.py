"""Dispatch, argument parsing, and the validators the script delegates.

Everything reachable without root: what the script does before it decides to
provision anything. Includes the two lint gates on the script itself.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support.install import (
    BASH,
    SCRIPT,
    evaluate_os_gate,
    host_helper,
    run_script,
)

from blitzecdn.core.config.settings import Settings
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.host import install_inputs

# --- lint --------------------------------------------------------------------


def test_shellcheck_is_clean():
    """CI runs this too; failing here first makes the cause obvious."""
    candidate = Path(sys.executable).parent / "shellcheck"
    shellcheck = str(candidate) if candidate.exists() else shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck-py is not installed in this environment")
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [shellcheck, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def test_installer_has_valid_shell_syntax():
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --- dispatch ----------------------------------------------------------------


def test_no_arguments_show_help_and_the_development_workflow(tmp_path):
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, str(SCRIPT)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "just install" in result.stdout
    assert "uv run blitzecdn setup" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("argument", ["install", "--bogus", "bogus"])
def test_unknown_subcommands_are_rejected_with_guidance(argument):
    result = run_script(argument)
    assert result.returncode == 2
    assert "unknown subcommand" in result.stderr
    assert "just install" in result.stderr


def test_root_help_lists_every_subcommand():
    result = run_script("--help")
    assert result.returncode == 0
    assert "standalone" in result.stdout
    assert "update" in result.stdout
    assert "--uninstall" in result.stdout
    assert "blitzecdn-install" in result.stdout
    for removed in ("--fresh", "upgrade"):
        assert removed not in result.stdout
    assert "BLITZECDN_CONTROL_PLANE_IMAGE" in result.stdout
    assert "BLITZECDN_WRAPPER_DIR" not in result.stdout


@pytest.mark.parametrize("form", ["help", "-h", "--help"])
def test_every_help_form_exits_zero(form: str):
    assert run_script(form).returncode == 0


@pytest.mark.parametrize("subcommand", ["standalone", "update"])
def test_privileged_subcommands_refuse_to_run_unprivileged(subcommand: str):
    result = run_script(subcommand)
    assert result.returncode == 1
    assert "sudo" in result.stderr
    assert result.stdout == ""


# --- argument parsing (runs before the root check, so it is reachable) -------


@pytest.mark.parametrize(
    ("subcommand", "option"),
    [
        ("standalone", "--admin-cidr"),
        ("standalone", "--email"),
        ("standalone", "--public-address"),
        ("standalone", "--allowed-ips"),
        ("update", "--allowed-ips"),
    ],
)
def test_options_requiring_a_value_reject_a_missing_one(subcommand: str, option: str):
    result = run_script(subcommand, option)
    assert result.returncode == 2
    assert "needs a value" in result.stderr


def test_update_does_not_take_a_ref():
    """A server follows its own release line; `update` takes no ref."""
    result = run_script("update", "--ref", "v3.1.0")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


@pytest.mark.parametrize("subcommand", ["standalone", "update"])
def test_unknown_options_are_rejected_with_usage(subcommand: str):
    result = run_script(subcommand, "--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr
    assert "Usage:" in result.stderr


@pytest.mark.parametrize(
    ("subcommand", "expected"),
    [
        ("standalone", ["--admin-cidr CIDR", "--email ADDRESS", "--deploy"]),
        ("update", ["--yes", "--no-backup", "--major"]),
    ],
)
def test_subcommand_help_does_not_require_root(subcommand: str, expected: list[str]):
    result = run_script(subcommand, "--help")
    assert result.returncode == 0
    for fragment in expected:
        assert fragment in result.stdout


# --- the validators the script delegates to Python ---------------------------


@pytest.mark.parametrize(
    ("distribution", "version", "accepted"),
    [
        # Debian 12 is not a supported controller: it ships Python 3.11 and the
        # control plane needs 3.12+.
        ("Debian", "12", False),
        ("Debian", "13", True),
        ("Debian", "14", True),
        ("Debian", "11", False),
        ("Ubuntu", "24", True),
        ("Ubuntu", "26", True),
        ("Ubuntu", "22", False),
        ("Fedora", "42", False),
    ],
)
def test_operating_system_gate(distribution: str, version: str, accepted: bool):
    """The gate is the role's assert now, so evaluate the expression it asserts."""
    assert evaluate_os_gate(distribution, version) is accepted


@pytest.mark.parametrize(
    ("cidr", "accepted"),
    [
        ("203.0.113.8/32", True),
        ("10.0.0.0/8", True),
        ("2001:db8::/32", True),
        ("203.0.113.8", True),
        ("not-a-cidr", False),
        ("203.0.113.8/99", False),
        ("", False),
    ],
)
def test_admin_cidr_validation(cidr: str, accepted: bool):
    assert (
        _install_inputs("standalone", cidr, "operator@example.com") == 0
    ) is accepted


@pytest.mark.parametrize(
    ("email", "accepted"),
    [
        ("operator@example.com", True),
        ("no-at-sign", False),
        ("two@at@signs", False),
        ("has space@example.com", False),
        ("", False),
    ],
)
def test_acme_email_validation(email: str, accepted: bool):
    assert (_install_inputs("standalone", "203.0.113.8/32", email) == 0) is accepted


def _install_inputs(*arguments: str) -> int:
    """The helper's exit status, run in-process so its lines are measured."""
    try:
        return install_inputs.main(arguments)
    except SystemExit as exit_:
        return int(exit_.code or 0)


def _validate_allowed_ips(value: str) -> int:
    return _install_inputs("allowed-ips", value)


def test_install_inputs_runs_the_way_the_installer_starts_it():
    """`install.sh` runs ``python -I -m`` in the image; isolation must not hide
    the package the helper now imports its rule from."""
    accepted = host_helper("install_inputs", "allowed-ips", "203.0.113.8/32")
    assert accepted.returncode == 0, accepted.stderr
    refused = host_helper("install_inputs", "allowed-ips", "2001:db8::/32")
    assert refused.returncode == 1
    assert refused.stderr.startswith("error: --allowed-ips entry")


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("203.0.113.8/32", True),
        ("203.0.113.8", True),
        ("198.51.100.0/24", True),
        ("10.0.0.0/8,192.0.2.1", True),
        ("203.0.113.8/32, 198.51.100.0/24", True),
        # The listener is IPv4, so an IPv6 entry could only ever be a list that
        # admits nobody.
        ("2001:db8::/32", False),
        # One address or 256 of them, and the silent reading is the wide one.
        ("203.0.113.8/24", False),
        ("not-an-ip", False),
        ("203.0.113.8/99", False),
        ("", False),
        ("203.0.113.8/32,", False),
    ],
)
def test_allowed_ips_validation(value: str, accepted: bool):
    assert (_validate_allowed_ips(value) == 0) is accepted


def test_allowed_ips_is_refused_before_anything_is_installed(capsys):
    """The refusal has to name the two readings, not just say no.

    Refusing rather than masking makes the widening a choice the operator states.
    """
    assert _validate_allowed_ips("203.0.113.8/24") != 0
    stderr = capsys.readouterr().err
    assert "--allowed-ips entry '203.0.113.8/24'" in stderr
    assert "203.0.113.0/24" in stderr
    assert "203.0.113.8/32" in stderr


@pytest.mark.parametrize(
    "value",
    [
        "203.0.113.8/32",
        "203.0.113.8",
        "10.0.0.0/8",
        "2001:db8::/32",
        "203.0.113.8/24",
        "10.1.2.3/8",
        "not-an-ip",
        "203.0.113.8/32,",
    ],
)
def test_the_installer_and_the_setting_it_seeds_agree(tmp_path, value: str):
    """One rule, two readers, and the flag runs first.

    `--allowed-ips` is checked in the image before the host converges; `Settings`
    checks the same list when the API reads it. Both call `allowed_ip_networks`,
    so this holds by construction; it stays to catch a reader that stops calling
    it, since then the flag could provision a server that cannot start. An empty
    environment value returns a server to loopback, but an empty flag is refused,
    so it is not listed here.
    """
    accepted_by_installer = _validate_allowed_ips(value) == 0
    try:
        Settings.from_environment({"BLITZE_ALLOWED_IPS": value}, project_dir=tmp_path)
        accepted_by_setting = True
    except ConfigurationError:
        accepted_by_setting = False
    assert accepted_by_installer is accepted_by_setting
