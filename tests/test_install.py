"""Behavioral tests for install.sh.

The privileged subcommands refuse to run as a normal user, so the paths that
provision a server cannot be executed here. What *can* be executed is
everything that decides whether to provision: dispatch, argument parsing, and
the validators the script shells out to Python for. Those run directly against
the source extracted from the script, so the assertions describe behavior
rather than the text that happens to implement it.

A handful of structural assertions remain at the bottom. Each one guards a
property of the script's *shape* that no non-root execution can reach, and each
says why.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "install.sh"
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


def _section(name: str) -> str:
    """Return one subcommand's body.

    Assertions about what a subcommand must never do have to be scoped to it:
    the three installers share a file now, and `standalone` legitimately runs a
    deployment that `update` must never run.
    """
    remainder = _script()[_script().index(f"cmd_{name}() {{") :]
    banner = re.search(r"\n# -{10,}", remainder)
    return remainder if banner is None else remainder[: banner.start()]


def _embedded_python(marker: str) -> str:
    """Return the `python3 - <<'PY'` heredoc containing `marker`.

    Selected by content rather than position: adding a heredoc earlier in the
    script must not silently repoint these tests at different code.
    """
    blocks = [
        block
        for block in re.findall(r"<<'PY'\n(.*?)\nPY\n", _script(), re.DOTALL)
        if marker in block
    ]
    assert len(blocks) == 1, f"expected exactly one heredoc containing {marker!r}"
    return blocks[0]


def _run_embedded(source: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - interpreter is sys.executable
        [sys.executable, "-", *arguments],
        input=source,
        check=False,
        capture_output=True,
        text=True,
    )


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
    """The upgrade path depends on this.

    `update` checks out the new release and runs its install.sh with no
    arguments, from a copy of the *previous* release's script. A default form
    that required an argument would strand every installed host.
    """
    result = _run("--bogus")
    assert result.returncode == 2
    assert "takes no arguments" in result.stderr


def test_root_help_lists_every_subcommand():
    result = _run("--help")
    assert result.returncode == 0
    assert "standalone" in result.stdout
    assert "update" in result.stdout
    assert "BLITZECDN_EDGE_PATH" in result.stdout


@pytest.mark.parametrize("form", ["help", "-h", "--help"])
def test_every_help_form_exits_zero(form: str):
    assert _run(form).returncode == 0


@pytest.mark.parametrize("subcommand", ["standalone", "update"])
def test_privileged_subcommands_refuse_to_run_unprivileged(subcommand: str):
    """Neither may do anything before establishing it is root."""
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
        ("update", "--version"),
    ],
)
def test_options_requiring_a_value_reject_a_missing_one(subcommand: str, option: str):
    result = _run(subcommand, option)
    assert result.returncode == 2
    assert "needs a value" in result.stderr


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
        ("update", ["--version vX.Y.Z", "--check", "--yes"]),
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
        ("debian", "12", True),
        ("debian", "13", True),
        ("debian", "11", False),
        ("ubuntu", "24.04", True),
        ("ubuntu", "26.04", True),
        ("ubuntu", "22.04", False),
    ],
)
def test_operating_system_gate(distribution: str, version: str, accepted: bool):
    result = _run_embedded(_embedded_python("minimum = "), distribution, version)
    assert (result.returncode == 0) is accepted


def test_operating_system_gate_rejects_an_unparsable_version():
    result = _run_embedded(_embedded_python("minimum = "), "debian", "not-a-number")
    assert result.returncode != 0
    assert "invalid operating-system version" in result.stderr


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
    result = _run_embedded(_embedded_python("ip_network"), cidr, "operator@example.com")
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
    result = _run_embedded(_embedded_python("ip_network"), "203.0.113.8/32", email)
    assert (result.returncode == 0) is accepted


# --- release-tag selection ---------------------------------------------------


def _tag_pipeline(ls_remote_output: str) -> list[str]:
    """Run the script's own tag-extraction pipeline over fake ls-remote output."""
    match = re.search(r"(sed -n 's#\.\*refs/tags/.*?#p') \|\n\s*(sort -V)", _script())
    assert match is not None, "tag extraction pipeline not found in install.sh"
    pipeline = f"{match.group(1)} | {match.group(2)}"
    result = subprocess.run(  # noqa: S603 - pipeline extracted from the script
        [BASH, "-c", pipeline],
        input=ls_remote_output,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_tag_selection_keeps_only_stable_releases_and_orders_them_numerically():
    versions = _tag_pipeline(
        "aaa\trefs/tags/v1.2.7\n"
        "bbb\trefs/tags/v1.10.0\n"
        "ccc\trefs/tags/v1.2.9\n"
        "ddd\trefs/tags/v2.0.0-rc1\n"
        "eee\trefs/tags/nightly\n"
        "fff\trefs/tags/v1.2\n"
    )
    # Prereleases and non-version tags are dropped, and 1.10.0 sorts above
    # 1.2.9 rather than lexically below it.
    assert versions == ["v1.2.7", "v1.2.9", "v1.10.0"]
    assert versions[-1] == "v1.10.0"


def test_tag_membership_test_does_not_accept_a_prefix_match():
    """`v1.2.9` must not be considered present in a list holding `v1.2.90`."""
    script = (
        "remote_versions=(v1.2.90 v1.3.0); target_version=v1.2.9\n"
        'if [[ " ${remote_versions[*]} " != *" ${target_version} "* ]]; then\n'
        "  echo absent\nelse\n  echo present\nfi\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed executable, literal script
        [BASH, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "absent"
    # The same comparison, in the same form, must be what the script uses.
    assert '[[ " ${remote_versions[*]} " != *" ${target_version} "* ]]' in _script()


# --- structural guarantees no unprivileged run can reach ---------------------


def test_update_never_deploys():
    """A deploy rewrites edge configuration; an update must only change code."""
    update = _section("update")
    assert (
        re.search(r"(?m)^\s*(?:run_blitzecdn|/usr/local/bin/blitzecdn) deploy", update)
        is None
    )
    assert "/usr/local/bin/blitzecdn doctor" in update


def test_update_reinstalls_through_the_argument_free_default_form():
    """Both the success path and the rollback path must call it bare."""
    update = _section("update")
    assert update.count('runuser -u blitzecdn -- "${INSTALL_DIR}/install.sh"') == 2
    # A redirection may follow, but never an argument.
    assert (
        re.search(
            r'runuser -u blitzecdn -- "\$\{INSTALL_DIR\}/install\.sh" +[\w-]',
            update,
        )
        is None
    )


def test_update_runs_from_a_private_copy_before_rewriting_the_checkout():
    """Checking out a release overwrites this file while bash is reading it."""
    update = _section("update")
    assert "BLITZECDN_UPDATE_REEXEC" in update
    assert 'install -m 0700 -- "$0" "${updater_copy}"' in update
    assert '"${original_args[@]}"' in update


def test_update_backs_up_state_and_rolls_back_on_failure():
    update = _section("update")
    assert "backup_items=(opt/blitzecdn/.state)" in update
    assert "etc/blitzecdn" in update
    assert "trap rollback ERR" in update
    assert 'repo_git checkout --detach "${previous_commit}"' in update


def test_update_scopes_git_safe_directory_without_changing_global_config():
    script = _script()
    assert 'git -c safe.directory="${INSTALL_DIR}" "$@"' in script
    assert "git config --global" not in script


def test_standalone_keeps_the_management_api_on_loopback():
    script = _script()
    assert "ssh -L 8000:127.0.0.1:8000" in script
    assert "--host 0.0.0.0" not in script


def test_standalone_defaults_to_no_deployment():
    standalone = _section("standalone")
    assert "run_deploy=0" in standalone
    assert "Initial deployment skipped (safe default)" in standalone


def test_standalone_guards_existing_sites_from_empty_desired_state():
    standalone = _section("standalone")
    assert "managed_registry=/etc/nginx/blitzecdn-managed-sites" in standalone
    assert "this edge has managed sites but desired state is empty" in standalone


def test_standalone_does_not_take_ownership_of_role_managed_home_data():
    """A blanket chown of the service home breaks role-managed ACME paths."""
    script = _script()
    assert "chown -R blitzecdn:blitzecdn /var/lib/blitzecdn" not in script
    assert "for service_path in .ansible .cache .ssh" in script
    assert "install -d -m 0751 -o blitzecdn -g blitzecdn /var/lib/blitzecdn" in script


def test_installer_forces_git_collection_reinstall_for_prefixed_tags():
    """ansible-core cannot compare a v-prefixed ref to a numeric manifest."""
    assert '-r ansible/requirements.yml -p "${collections_path}" --force' in _script()


def test_installer_preserves_rather_than_deletes_an_incomplete_virtualenv():
    script = _script()
    assert "! -x .venv/bin/python || ! -x .venv/bin/pip" in script
    assert ".venv.invalid.$(date -u +%Y%m%dT%H%M%SZ)" in script
