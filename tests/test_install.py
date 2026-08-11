"""Behavioral tests for install.sh.

The privileged subcommands refuse to run as a normal user, so the paths that
provision a server cannot be executed directly here. What *can* be executed is
everything that decides whether to provision: dispatch, argument parsing, and
the validators the script shells out to Python for. Those run directly against
the source extracted from the script, so the assertions describe behavior
rather than the text that happens to implement it.

The destructive paths (``--uninstall`` and ``--fresh``) are exercised for real
in a sandbox: the script is copied with every path it touches redirected under
a temp directory and the root check neutralised, and the privileged commands
(systemctl, userdel, getent, nginx, git) are stubbed on ``PATH``. That lets a
test verify what is actually removed and that the reinstall takes the same
path as a brand-new server, without touching the host.

A handful of structural assertions remain at the bottom. Each one guards a
property of the script's *shape* that no sandboxed run can reach, and each says
why.
"""

import os
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


def _function(name: str) -> str:
    """Return one named function's body, braces stripped.

    `_section` only reaches the cmd_* entry points. The cleanup and
    confirmation helpers that sit between them need their own extractor, and
    its body is bounded the same way the script bounds it: by a closing brace
    at column zero.
    """
    script = _script()
    start = script.index(f"{name}() {{") + len(f"{name}() {{")
    closing = script.index("\n}\n", start)
    return script[start:closing]


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


# --- sandbox for the destructive paths ---------------------------------------
#
# `--uninstall` and `--fresh` need root and touch absolute system paths. The
# tests below run them for real inside a redirected copy of the script, with
# every privileged command stubbed, so the assertions are about what actually
# happens on a fake installation rather than the text that implements it.


def _instrument(sandbox: Path) -> tuple[Path, Path]:
    """Copy install.sh with every path redirected under ``sandbox/root``.

    The root check is replaced with a tautology so a normal test user can
    exercise the destructive paths; the stubbed commands below never touch the
    real system. Returns the script and the redirect root.
    """
    sandbox.mkdir(parents=True, exist_ok=True)
    root = sandbox / "root"
    script = sandbox / "install.sh"
    text = SCRIPT.read_text(encoding="utf-8")
    for real, redirected in (
        ("/etc/blitzecdn", root / "etc/blitzecdn"),
        ("/etc/sudoers.d", root / "etc/sudoers.d"),
        ("/etc/nginx", root / "etc/nginx"),
        ("/etc/systemd", root / "etc/systemd"),
        ("/etc/fail2ban", root / "etc/fail2ban"),
        ("/etc/ssh", root / "etc/ssh"),
        ("/etc/sysctl.d", root / "etc/sysctl.d"),
        ("/etc/GeoIP.conf", root / "etc/GeoIP.conf"),
        ("/var/cache/nginx", root / "var/cache/nginx"),
        ("/var/log/nginx", root / "var/log/nginx"),
        ("/var/backups/blitzecdn", root / "var/backups/blitzecdn"),
        ("/var/lib/blitzecdn", root / "var/lib/blitzecdn"),
        ("/home/deploy", root / "home/deploy"),
        ("/usr/local/bin/blitzecdn", root / "usr/local/bin/blitzecdn"),
        ("/opt/blitzecdn", root / "opt/blitzecdn"),
    ):
        text = text.replace(real, str(redirected))
    text = text.replace("[[ ${EUID} -eq 0 ]]", "[[ 0 -eq 0 ]]")
    script.write_text(text, encoding="utf-8")
    script.chmod(0o700)
    return script, root


def _stub_bin(sandbox: Path, root: Path) -> None:
    """Provide stand-ins for every privileged command the destructive paths run."""
    bindir = sandbox / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bindir / "userdel").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bindir / "nginx").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bindir / "getent").write_text(
        "#!/usr/bin/env bash\n"
        'case "$2" in\n'
        "  blitzecdn)\n"
        f'    echo "blitzecdn:x:999:999:BlitzeCDN:{root / "var/lib/blitzecdn"}'
        ':/usr/sbin/nologin"\n'
        "    ;;\n"
        "  deploy)\n"
        f'    echo "deploy:x:1000:1000:deploy:{root / "home/deploy"}:/bin/bash"\n'
        "    ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    # A stand-in that answers the fresh-rebuild questions and fabricates a
    # checkout whose install.sh proves the reinstall path ran.
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"remote get-url origin"*)\n'
        '    echo "https://github.com/misaf/blitze-cdn-cp.git"; exit 0 ;;\n'
        '  *"describe --tags --exact-match HEAD"*)\n'
        '    echo "v1.2.3"; exit 0 ;;\n'
        "  clone*)\n"
        '    target="${@: -1}"\n'
        '    mkdir -p "${target}"\n'
        "    cat > \"${target}/install.sh\" <<'FAKE'\n"
        "#!/usr/bin/env bash\n"
        'echo fresh-reinstalled > "${FRESH_REINSTALL_MARKER}"\n'
        "exit 0\n"
        "FAKE\n"
        '    chmod +x "${target}/install.sh"\n'
        "    exit 0\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for path in bindir.iterdir():
        path.chmod(0o700)


def _fake_installation(root: Path, *, with_git: bool = True) -> list[Path]:
    """Create every artifact a standalone install owns on the host."""
    owned = [
        root / "opt/blitzecdn/install.sh",
        root / "opt/blitzecdn/.state/control-plane.db",
        root / "opt/blitzecdn/.state/desired-state.yml",
        root / "opt/blitzecdn/.state/certificates/example-cdn/fullchain.pem",
        root
        / "opt/blitzecdn/.state/collections/ansible_collections/blitzecdn"
        / "edge/MANIFEST.json",
        root / "opt/blitzecdn/.state/letsencrypt/config",
        root / "opt/blitzecdn/.state/ansible-local",
        root / "opt/blitzecdn/.env",
        root / "opt/blitzecdn/.venv/bin/python",
        root / "opt/blitzecdn/blitzecdn.toml",
        root / "opt/blitzecdn/log/run.log",
        root / "etc/blitzecdn/blitzecdn.env",
        root / "etc/blitzecdn/firewall-rules",
        root / "etc/systemd/system/blitzecdn-api.service",
        root / "etc/systemd/system/blitzecdn-cert-renew.service",
        root / "etc/systemd/system/blitzecdn-cert-renew.timer",
        root / "etc/systemd/system/blitzecdn-drift.service",
        root / "etc/systemd/system/blitzecdn-drift.timer",
        root / "etc/systemd/system/blitzecdn-geoipupdate.service",
        root / "etc/systemd/system/blitzecdn-geoipupdate.timer",
        root / "usr/local/bin/blitzecdn",
        root / "etc/sudoers.d/blitzecdn-deploy",
        root / "var/backups/blitzecdn/20250101T000000Z-v1.7.2.tar.gz",
        root / "etc/nginx/blitzecdn-managed-sites",
        root / "etc/nginx/conf.d/blitzecdn-cache.conf",
        root / "etc/nginx/conf.d/blitzecdn-geoip.conf",
        root / "etc/nginx/conf.d/blitzecdn-status.conf",
        root / "etc/nginx/sites-available/example-cdn.conf",
        root / "etc/nginx/sites-enabled/example-cdn.conf",
        root / "var/cache/nginx/blitzecdn/cache-data",
        root / "etc/systemd/resolved.conf.d/blitzecdn.conf",
        root / "var/log/nginx/blitzecdn-access.log",
        root / "etc/fail2ban/jail.d/blitzecdn-sshd.local",
        root / "etc/ssh/sshd_config.d/50-blitzecdn.conf",
        root / "etc/sysctl.d/60-blitzecdn.conf",
        root / "etc/GeoIP.conf",
        root / "var/lib/blitzecdn/.ssh/known_hosts",
        root / "var/lib/blitzecdn/.ssh/config",
        root / "var/lib/blitzecdn/acme/.well-known/acme-challenge/token",
        root / "home/deploy/.ssh/authorized_keys",
    ]
    if with_git:
        owned.append(root / "opt/blitzecdn/.git/HEAD")
    for path in owned:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Managed by BlitzeCDN. Local edits are overwritten.\nx\n",
            encoding="utf-8",
        )
    return owned


def _fake_unrelated(root: Path) -> list[Path]:
    """Files a neighbouring admin could own that cleanup must leave alone."""
    unrelated = [
        root / "etc/nginx/sites-available/my-own-site.conf",
        root / "etc/nginx/sites-enabled/my-own-site.conf",
        root / "etc/ssh/sshd_config.d/10-admin.conf",
        root / "etc/fail2ban/jail.d/custom.local",
        root / "etc/sudoers.d/admin",
        root / "usr/local/bin/other-tool",
        root / "etc/unrelated.conf",
        root / "home/other-user/notes.txt",
    ]
    for path in unrelated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("precious user data\n", encoding="utf-8")
    return unrelated


def _run_sandboxed(
    script: Path,
    *arguments: str,
    input: str | None = None,  # noqa: A002 - mirrors subprocess.run
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{script.parent / 'bin'}:{environment['PATH']}"
    if env_extra:
        environment.update(env_extra)
    return subprocess.run(  # noqa: S603 - fixed executable and sandboxed script
        [BASH, str(script), *arguments],
        check=False,
        capture_output=True,
        text=True,
        input=input,
        env=environment,
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
    assert "--fresh" in result.stdout
    assert "--uninstall" in result.stdout
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


# --- fresh rebuild and uninstall: dispatch ------------------------------------


def test_dispatch_recognizes_the_destructive_subcommands():
    script = _script()
    assert re.search(r"\(--uninstall|--uninstall\)|cmd_uninstall", script)
    assert re.search(r"\(--fresh|--fresh\)|cmd_fresh", script)


@pytest.mark.parametrize("subcommand", ["--uninstall", "--fresh"])
def test_destructive_subcommands_refuse_to_run_unprivileged(subcommand):
    result = _run(subcommand)
    assert result.returncode == 1
    assert "sudo" in result.stderr


@pytest.mark.parametrize(
    ("subcommand", "help_fragment"),
    [
        ("--uninstall", "--yes"),
        ("--fresh", "--admin-cidr"),
    ],
)
def test_destructive_subcommand_help_does_not_require_root(subcommand, help_fragment):
    result = _run(subcommand, "--help")
    assert result.returncode == 0
    assert help_fragment in result.stdout


def test_uninstall_rejects_unknown_options():
    result = _run("--uninstall", "--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr


# --- fresh rebuild and uninstall: structural guarantees ----------------------


def test_destructive_commands_survive_deleting_their_own_directory():
    """Both remove $INSTALL_DIR, so both must continue from a copied script."""
    for name in ("uninstall", "fresh"):
        section = _section(name)
        assert "BLITZECDN_UNINSTALL_REEXEC" in section
        assert 'install -m 0700 -- "$0" "${uninstall_copy}"' in section or (
            'install -m 0700 -- "$0" "${fresh_copy}"' in section
        )
        assert '"${original_args[@]}"' in section
        assert "trap 'rm -f -- \"$0\"' EXIT" in section


def test_cleanup_removes_every_owned_artifact():
    cleanup = _function("remove_blitzecdn_artifacts")
    for fragment in (
        'systemctl stop "${SERVICES[@]}" "${EDGE_MANAGED_UNITS[@]}"',
        'systemctl disable "${SERVICES[@]}" "${EDGE_MANAGED_UNITS[@]}"',
        'rm -f -- "/etc/systemd/system/${unit}"',
        "systemctl daemon-reload",
        'rm -rf -- "${CONFIG_DIR}" "${CLI_WRAPPER}" "${SUDOERS_FILE}" "${BACKUP_DIR}"',
        'rm -rf -- "${INSTALL_DIR}"',
        "remove_service_account blitzecdn /var/lib/blitzecdn",
        "remove_service_account deploy /home/deploy",
        'rm -rf -- "${EDGE_MANAGED_FILES[@]}"',
        'grep -q "${NGINX_MARKER}" "${conf}"',
    ):
        assert fragment in cleanup


def test_cleanup_tolerates_a_partial_or_already_removed_installation():
    cleanup = _function("remove_blitzecdn_artifacts")
    assert "|| true" in cleanup
    assert "|| continue" in cleanup
    assert "2>/dev/null" in cleanup
    assert "[[ -f ${conf} ]] || continue" in cleanup


def test_cleanup_removes_service_accounts_and_homes():
    cleanup = _function("remove_blitzecdn_artifacts")
    assert "remove_service_account blitzecdn /var/lib/blitzecdn" in cleanup
    assert "remove_service_account deploy /home/deploy" in cleanup


def test_cleanup_only_touches_marker_matched_nginx_and_owned_drop_ins():
    """Unrelated nginx config and ssh drop-ins must survive because cleanup
    removes marker-matched sites and the exact owned paths, not whole dirs."""
    cleanup = _function("remove_blitzecdn_artifacts")
    nginx_glob = (
        "for conf in /etc/nginx/conf.d/*.conf /etc/nginx/sites-available/*.conf"
    )
    assert nginx_glob in cleanup
    assert 'grep -q "${NGINX_MARKER}" "${conf}"' in cleanup
    assert "rm -rf -- /etc/nginx" not in cleanup
    assert "rm -rf -- /etc/ssh" not in cleanup
    assert 'rm -rf -- "${EDGE_MANAGED_FILES[@]}"' in cleanup
    assert "/etc/ssh/sshd_config.d/50-blitzecdn.conf" in _script()


def test_destructive_commands_require_confirmation():
    for name in ("uninstall", "fresh"):
        section = _section(name)
        assert "confirm_destructive" in section
        assert "assume_yes=0" in section
        assert "--yes)" in section
    confirm = _function("confirm_destructive")
    assert "read -r -p" in confirm
    assert "Cancelled." in confirm


def test_fresh_reuses_the_same_cleanup_as_uninstall():
    fresh = _section("fresh")
    assert "remove_blitzecdn_artifacts" in fresh
    assert 'run "sudo ./install.sh --uninstall"' not in fresh


def test_fresh_reinstalls_the_running_release_like_a_new_server():
    fresh = _section("fresh")
    assert "remote get-url origin" in fresh
    assert "describe --tags --exact-match HEAD" in fresh
    assert 'git clone --depth 1 --branch "${revision}"' in fresh
    assert 'git -C "${INSTALL_DIR}" checkout --detach "${revision}"' in fresh
    assert '"${INSTALL_DIR}/install.sh" standalone ' in fresh
    assert '${fresh_args[@]+"${fresh_args[@]}"}' in fresh


def test_fresh_refuses_to_rebuild_without_a_source_checkout():
    fresh = _section("fresh")
    assert "${INSTALL_DIR} is not a Git checkout" in fresh


# --- fresh rebuild and uninstall: sandboxed behaviour ------------------------


def test_uninstall_removes_owned_artifacts_and_leaves_unrelated(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    owned = _fake_installation(root)
    unrelated = _fake_unrelated(root)

    result = _run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    for path in owned:
        assert not path.exists(), f"owned artifact survived uninstall: {path}"
    for path in unrelated:
        assert path.exists(), f"unrelated file was removed: {path}"


def test_uninstall_asks_for_confirmation_and_can_be_cancelled(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    owned = _fake_installation(root)

    cancelled = _run_sandboxed(script, "--uninstall", input="n\n")
    assert cancelled.returncode == 0
    assert "Cancelled" in cancelled.stdout
    assert all(path.exists() for path in owned)


def test_uninstall_is_idempotent_and_safe_to_rerun(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root)

    first = _run_sandboxed(script, "--uninstall", "--yes")
    second = _run_sandboxed(script, "--uninstall", "--yes")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr


def test_uninstall_works_even_when_the_installation_directory_is_already_deleted(
    tmp_path: Path,
):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    owned = _fake_installation(root)

    shutil.rmtree(root / "opt/blitzecdn")

    result = _run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    for path in owned:
        assert not path.exists(), f"owned artifact survived uninstall: {path}"


def test_fresh_rebuild_removes_then_reinstalls_like_a_brand_new_server(
    tmp_path: Path,
):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    marker = tmp_path / "fresh-reinstalled"
    _fake_installation(root)

    result = _run_sandboxed(
        script,
        "--fresh",
        "--yes",
        env_extra={"FRESH_REINSTALL_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists(), "the fresh install.sh never ran"
    assert marker.read_text().strip() == "fresh-reinstalled"
    assert (root / "opt/blitzecdn/install.sh").exists(), "no fresh checkout was created"


def test_fresh_refuses_to_rebuild_without_a_git_checkout(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root, with_git=False)

    result = _run_sandboxed(script, "--fresh", "--yes")

    assert result.returncode == 1
    assert "is not a Git checkout" in result.stderr
