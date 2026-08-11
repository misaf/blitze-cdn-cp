"""Behavioral tests for bootstrap.sh.

The script clones a release and provisions a server, so almost all of it needs
root and a network. What can be executed here is everything that runs *before*
either is touched: argument parsing, the required-flag check, and the version
format. Those run against the real script.

The rest is asserted structurally, and each of those assertions guards a
property that install.sh depends on. A bootstrap that produced an unpacked
tarball, or cloned somewhere other than /opt/blitzecdn, would install once and
then leave the host unable to update itself — the failure would appear weeks
later, on somebody else's machine.
"""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "bootstrap.sh"
BASH = "/bin/bash"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _code() -> str:
    """The script without its full-line comments.

    "Must not contain" assertions have to read what runs, not what the file says
    about itself — this script explains in prose exactly which mistakes it
    avoids, and naming one would otherwise fail the test guarding against it.
    """
    return "\n".join(
        line for line in _script().splitlines() if not line.lstrip().startswith("#")
    )


def test_help_documents_the_required_flags_and_the_upgrade_path():
    result = _run("--help")

    assert result.returncode == 0
    assert "--admin-cidr" in result.stdout
    assert "--email" in result.stdout
    # Someone re-running the one-liner to upgrade is the likeliest mistake.
    assert "install.sh update" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ((), "--admin-cidr is required"),
        (("--admin-cidr", "203.0.113.8/32"), "--email is required"),
        (("--email", "ops@example.com"), "--admin-cidr is required"),
    ],
)
def test_missing_required_flags_fail_before_anything_is_installed(
    arguments: tuple[str, ...], message: str
):
    """Reported here rather than by install.sh, which only runs after the clone.

    A missing flag caught late would leave a checkout behind that the next run
    of this script refuses to overwrite.
    """
    result = _run(*arguments)

    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize("version", ["1.2.3", "v1.2", "latest", "v1.2.3.4"])
def test_a_malformed_version_is_rejected_as_invalid_input(version: str):
    """Exit 2 without needing root or a network round trip to say so."""
    result = _run(
        "--admin-cidr", "203.0.113.8/32", "--email", "o@e.com", "--version", version
    )

    assert result.returncode == 2
    assert "vX.Y.Z format" in result.stderr


def test_version_requires_a_value():
    result = _run("--admin-cidr", "203.0.113.8/32", "--email", "o@e.com", "--version")

    assert result.returncode == 2
    assert "--version needs a value" in result.stderr


def test_valid_arguments_reach_the_host_check_and_stop_without_root():
    """Proves the parser accepts a real invocation, and that root is required."""
    result = _run("--admin-cidr", "203.0.113.8/32", "--email", "ops@example.com")

    assert result.returncode == 1
    assert "run this with sudo" in result.stderr


def test_it_clones_a_git_checkout_to_the_only_directory_standalone_accepts():
    """install.sh standalone dies anywhere but /opt/blitzecdn."""
    script = _script()
    assert "readonly INSTALL_DIR=/opt/blitzecdn" in script
    assert re.search(r'git clone --depth 1 --branch "\$\{version\}"', script)


def test_it_never_installs_from_an_archive():
    """`update` and `--fresh` refuse to run without /opt/blitzecdn/.git.

    A tarball would install once and strand the host on that release forever.
    """
    script = _code()
    for archive in ("tar -x", "unzip", "codeload", "/archive/", "tarball"):
        assert archive not in script, f"{archive} would produce a non-Git checkout"


def test_it_pins_a_release_tag_rather_than_a_moving_branch():
    script = _script()
    assert "refs/tags" in script
    # The documented production guidance is to install a tested tag; a bootstrap
    # that cloned the default branch would contradict it on the path most
    # operators actually use.
    assert "--branch main" not in script
    assert "--branch 1.x" not in script


def test_it_reports_what_git_said_when_the_repository_cannot_be_read():
    """Otherwise "repository not found" is reported as "no release tags"."""
    resolve = _script()[_script().index("resolve_version() {") :]
    assert "2>&1" in resolve
    assert "cannot read release tags" in resolve
    assert "2>/dev/null" not in resolve.split("\n}\n")[0]


def test_it_refuses_to_run_on_a_host_that_already_has_an_installation():
    script = _script()
    assert "already exists; this script only makes new installations" in script
    for alternative in ("install.sh update", "install.sh --fresh"):
        assert alternative in script


def test_it_verifies_the_tag_against_the_package_version():
    """The same mis-cut-release check `install.sh update` makes."""
    script = _script()
    assert "contains package version" in script
    # And it must not leave the bad checkout behind to block a retry.
    assert re.search(
        r'rm -rf -- "\$\{INSTALL_DIR\}"\n\s*die 1 "error: \$\{version\}', script
    )


def test_it_does_not_duplicate_what_the_installer_and_role_own():
    """Bootstrap is a bootstrap. Host state belongs to blitzecdn_controlplane."""
    script = _code()
    for owned in (
        "useradd",
        "visudo",
        "ssh-keygen",
        "ssh-keyscan",
        "systemctl",
        "sudoers",
    ):
        assert owned not in script, f"{owned} belongs to the control-plane role"
    assert "exec" in script and "install.sh" in script


def test_it_hands_over_with_exec_so_the_exit_code_survives():
    """The CLI's documented exit codes have to reach whoever ran the one-liner."""
    assert re.search(
        r'exec "\$\{INSTALL_DIR\}/install\.sh" standalone "\$\{passthrough\[@\]\}"',
        _script(),
    )
