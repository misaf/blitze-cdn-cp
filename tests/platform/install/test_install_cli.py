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
from install_support import (
    BASH,
    SCRIPT,
    _embedded_python,
    _evaluate_os_gate,
    _run,
    _run_embedded,
)

from blitzecdn.core.config.settings import Settings
from blitzecdn.core.exceptions import ConfigurationError

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


def test_default_form_takes_no_arguments():
    """Controller-only installation has an intentionally argument-free form."""
    result = _run("--bogus")
    assert result.returncode == 2
    assert "takes no arguments" in result.stderr


def test_root_help_lists_every_subcommand():
    result = _run("--help")
    assert result.returncode == 0
    assert "standalone" in result.stdout
    assert "update" in result.stdout
    assert "--fresh" in result.stdout
    assert "--uninstall" in result.stdout
    assert "BLITZECDN_WRAPPER_DIR" in result.stdout


@pytest.mark.parametrize("form", ["help", "-h", "--help"])
def test_every_help_form_exits_zero(form: str):
    assert _run(form).returncode == 0


@pytest.mark.parametrize("subcommand", ["standalone", "update"])
def test_privileged_subcommands_refuse_to_run_unprivileged(subcommand: str):
    result = _run(subcommand)
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
        ("upgrade", "--allowed-ips"),
    ],
)
def test_options_requiring_a_value_reject_a_missing_one(subcommand: str, option: str):
    result = _run(subcommand, option)
    assert result.returncode == 2
    assert "needs a value" in result.stderr


def test_update_does_not_take_a_ref():
    """A server follows its own release line; the target is not an operator's

    to choose. The option existed, so rejecting it explicitly is what tells an
    operator running the old command that the contract changed.
    """
    result = _run("update", "--ref", "v3.1.0")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


@pytest.mark.parametrize("subcommand", ["standalone", "update"])
def test_unknown_options_are_rejected_with_usage(subcommand: str):
    result = _run(subcommand, "--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr
    assert "Usage:" in result.stderr


@pytest.mark.parametrize(
    ("subcommand", "expected"),
    [
        ("standalone", ["--admin-cidr CIDR", "--email ADDRESS", "--deploy"]),
        ("update", ["--yes", "--no-backup"]),
    ],
)
def test_subcommand_help_does_not_require_root(subcommand: str, expected: list[str]):
    result = _run(subcommand, "--help")
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
    assert _evaluate_os_gate(distribution, version) is accepted


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
    result = _run_embedded(
        _embedded_python("--admin-cidr"), cidr, "operator@example.com"
    )
    assert (result.returncode == 0) is accepted


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
    result = _run_embedded(_embedded_python("--admin-cidr"), "203.0.113.8/32", email)
    assert (result.returncode == 0) is accepted


def _validate_allowed_ips(value: str) -> subprocess.CompletedProcess[str]:
    return _run_embedded(_embedded_python("--allowed-ips"), value)


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
    assert (_validate_allowed_ips(value).returncode == 0) is accepted


def test_allowed_ips_is_refused_before_anything_is_installed():
    """The refusal has to name the two readings, not just say no.

    An operator who writes a host-bit CIDR meant one of them, and the whole
    point of refusing rather than masking is that the widening is chosen out
    loud. A message that only reported "invalid" would leave them to guess
    which correction the installer would have applied.
    """
    result = _validate_allowed_ips("203.0.113.8/24")
    assert result.returncode != 0
    assert "203.0.113.0/24" in result.stderr
    assert "203.0.113.8/32" in result.stderr


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
    """One list, two validators, and the flag runs first.

    `--allowed-ips` is checked by the host interpreter before a package is
    installed, while `Settings` checks the same list when the API reads it. The
    rules are written twice because there is no virtualenv yet to import the
    first from; this is what keeps the second copy honest. A flag that accepted
    what the control plane later refused would provision a server that cannot
    start.

    The empty value is deliberately not in this list: an empty environment
    variable is how a server is returned to loopback, while an empty `--allowed-ips`
    is a flag given nothing and is refused.
    """
    accepted_by_installer = _validate_allowed_ips(value).returncode == 0
    try:
        Settings.from_environment({"BLITZE_ALLOWED_IPS": value}, project_dir=tmp_path)
        accepted_by_setting = True
    except ConfigurationError:
        accepted_by_setting = False
    assert accepted_by_installer is accepted_by_setting
