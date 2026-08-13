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
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

PROJECT_DIR = Path(__file__).parents[1]
SCRIPT = PROJECT_DIR / "install.sh"
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
    (bindir / "nginx").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    process_marker = sandbox / "processes-terminated"
    (bindir / "pgrep").write_text(
        "#!/usr/bin/env bash\n"
        f'[[ -n "${{PROCESS_HOLDERS:-}}" && ! -e "{process_marker}" ]] || exit 1\n'
        'echo "123 ssh"\n',
        encoding="utf-8",
    )
    (bindir / "pkill").write_text(
        f'#!/usr/bin/env bash\ntouch "{process_marker}"\n',
        encoding="utf-8",
    )
    # userdel and getent share a marker directory so the pair behaves like a
    # real account database: getent stops resolving an account once userdel has
    # removed it. A stub that always resolved both could not tell a successful
    # removal from a refused one, which is the distinction the uninstaller now
    # fails on. USERDEL_REFUSES makes userdel refuse, as it does for an account
    # that still has a process running.
    deleted = sandbox / "deleted"
    deleted.mkdir(exist_ok=True)
    (bindir / "userdel").write_text(
        "#!/usr/bin/env bash\n"
        'account="${@: -1}"\n'
        'if [[ -n "${USERDEL_REFUSES:-}" ]]; then\n'
        '  echo "userdel: user ${account} is currently used by process 1" >&2\n'
        "  exit 8\n"
        "fi\n"
        f'touch "{deleted}/${{account}}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "getent").write_text(
        "#!/usr/bin/env bash\n"
        f'[[ -e "{deleted}/$2" ]] && exit 2\n'
        'case "$2" in\n'
        "  blitzecdn)\n"
        f'    echo "blitzecdn:x:999:999:BlitzeCDN:{root / "var/lib/blitzecdn"}'
        ':/usr/sbin/nologin"\n'
        "    ;;\n"
        "  deploy)\n"
        f'    echo "deploy:x:1000:1000:deploy:{root / "home/deploy"}:/bin/bash"\n'
        "    ;;\n"
        "  *) exit 2 ;;\n"
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
        '    [[ -n "${FRESH_GIT_TAG-v1.2.3}" ]] || exit 1\n'
        '    echo "${FRESH_GIT_TAG-v1.2.3}"; exit 0 ;;\n'
        '  *"symbolic-ref --quiet --short HEAD"*)\n'
        '    [[ -n "${FRESH_GIT_BRANCH:-}" ]] || exit 1\n'
        '    echo "${FRESH_GIT_BRANCH}"; exit 0 ;;\n'
        '  *"rev-parse HEAD"*)\n'
        '    echo "0123456789abcdef0123456789abcdef01234567"; exit 0 ;;\n'
        "  clone*)\n"
        '    if [[ -n "${FRESH_GIT_CLONE_MARKER:-}" ]]; then\n'
        '      printf "%s\\n" "$*" > "${FRESH_GIT_CLONE_MARKER}"\n'
        "    fi\n"
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
        root / "opt/blitzecdn/.venv/bin/ansible-playbook",
        root / "opt/blitzecdn/ansible/playbooks/uninstall.yml",
        root / "opt/blitzecdn/blitzecdn.toml",
        root / "opt/blitzecdn/log/run.log",
        root / "etc/blitzecdn/blitzecdn.env",
        root / "etc/blitzecdn/firewall-rules",
        root / "etc/systemd/system/blitzecdn-api.service",
        root / "etc/systemd/system/blitzecdn-geoipupdate.service",
        root / "etc/systemd/system/blitzecdn-geoipupdate.timer",
        root / "usr/local/bin/blitzecdn",
        root / "etc/sudoers.d/blitzecdn-deploy",
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
    # The installer owns only invocation and final checkout removal. This
    # stand-in models a successful uninstall play by removing the system paths
    # from the fixture; it deliberately leaves /opt/blitzecdn for Bash.
    ansible = root / "opt/blitzecdn/.venv/bin/ansible-playbook"
    system_paths = [path for path in owned if root / "opt/blitzecdn" not in path.parents]
    ansible.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + "rm -rf -- "
        + " ".join(shlex.quote(str(path)) for path in system_paths)
        + "\n",
        encoding="utf-8",
    )
    ansible.chmod(0o700)
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
    """Controller-only installation has an intentionally argument-free form."""
    result = _run("--bogus")
    assert result.returncode == 2
    assert "takes no arguments" in result.stderr


def test_root_help_lists_every_subcommand():
    result = _run("--help")
    assert result.returncode == 0
    assert "standalone" in result.stdout
    assert "--fresh" in result.stdout
    assert "--uninstall" in result.stdout
    assert "BLITZECDN_WRAPPER_DIR" in result.stdout


@pytest.mark.parametrize("form", ["help", "-h", "--help"])
def test_every_help_form_exits_zero(form: str):
    assert _run(form).returncode == 0


@pytest.mark.parametrize("subcommand", ["standalone"])
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
    ],
)
def test_options_requiring_a_value_reject_a_missing_one(subcommand: str, option: str):
    result = _run(subcommand, option)
    assert result.returncode == 2
    assert "needs a value" in result.stderr


@pytest.mark.parametrize("subcommand", ["standalone"])
def test_unknown_options_are_rejected_with_usage(subcommand: str):
    result = _run(subcommand, "--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr
    assert "Usage:" in result.stderr


@pytest.mark.parametrize(
    ("subcommand", "expected"),
    [
        ("standalone", ["--admin-cidr CIDR", "--email ADDRESS", "--deploy"]),
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
        # Debian 12 is a supported edge platform but not a supported
        # controller: it ships Python 3.11 and the control plane needs 3.12+.
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


# --- structural guarantees no unprivileged run can reach ---------------------
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
    assert (
        "readonly MANAGED_SITE_REGISTRY=/etc/nginx/blitzecdn-managed-sites" in _script()
    )
    assert "[[ -s ${MANAGED_SITE_REGISTRY} ]]" in standalone
    assert "this edge has managed sites but desired state is empty" in standalone


def test_role_does_not_take_ownership_of_role_managed_home_data():
    """A blanket chown of the service home breaks role-managed ACME paths."""
    home = _role_task("Set the service home mode")
    assert home["ansible.builtin.file"]["mode"] == "0751"
    assert home["ansible.builtin.file"].get("recurse") is not True

    repair = _role_task("Repair service-home ownership left by an interrupted run")
    assert repair["loop"] == [".ansible", ".cache", ".ssh"]
    assert repair["ansible.builtin.file"]["recurse"] is True


def test_installer_installs_only_third_party_collections():
    """The BlitzeCDN roles ship in ansible/roles/; nothing pins or builds them.

    The --force that used to be here worked around ansible-core comparing a
    v-prefixed Git ref to a numeric manifest. With no Git-backed collection
    left, needing it again would mean the roles had been re-externalised.
    """
    script = _script()
    assert '-r ansible/requirements.yml -p "${collections_path}"' in script
    assert "--force" not in script
    assert "collection build" not in script


def test_installer_preserves_rather_than_deletes_an_incomplete_virtualenv():
    script = _script()
    # `pip` is no longer the tell: uv builds the environment and a server
    # install has no pip in it at all.
    assert "! -x .venv/bin/python" in script
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
        assert "reexec_from_private_copy BLITZECDN_UNINSTALL_REEXEC" in section
        # The copy must be taken before final self-removal deletes this file.
        assert section.index("reexec_from_private_copy") < section.index(
            "remove_installation_directory"
        )


def test_bash_delegates_all_system_teardown_to_ansible():
    script = _script()
    uninstall = _section("uninstall")
    assert "converge_uninstall" in uninstall
    assert uninstall.index("converge_uninstall") < uninstall.index(
        "remove_installation_directory"
    )
    for operation in ("systemctl", "userdel", "loginctl", "pkill", "nginx -t"):
        assert operation not in uninstall
    assert _function("remove_installation_directory").strip() == (
        'rm -rf -- "${INSTALL_DIR}"'
    )


def test_destructive_commands_require_confirmation():
    for name in ("uninstall", "fresh"):
        section = _section(name)
        assert "confirm_destructive" in section
        assert "assume_yes=0" in section
        assert "--yes)" in section
    confirm = _function("confirm_destructive")
    assert "read -r -p" in confirm
    assert "Cancelled." in confirm


def test_fresh_reuses_the_same_ansible_teardown_as_uninstall():
    fresh = _section("fresh")
    assert "converge_uninstall" in fresh
    assert "remove_installation_directory" in fresh
    assert 'run "sudo ./install.sh --uninstall"' not in fresh


def test_fresh_preserves_the_running_source_line_like_a_new_server():
    fresh = _section("fresh")
    assert "require_upstream_origin" in fresh
    assert "remote get-url origin" in _function("require_upstream_origin")
    assert "describe --tags --exact-match HEAD" in fresh
    assert "symbolic-ref --quiet --short HEAD" in fresh
    assert '[[ ${revision} != "2.x" ]]' in fresh
    assert 'git clone --branch "${revision}"' in fresh
    assert "git clone --depth 1" not in fresh
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


def test_uninstall_requires_the_ansible_runtime(tmp_path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root)

    shutil.rmtree(root / "opt/blitzecdn/.venv")
    result = _run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 1
    assert "Ansible is missing" in result.stderr


def test_uninstall_refuses_when_the_installation_directory_is_already_deleted(
    tmp_path: Path,
):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    owned = _fake_installation(root)

    shutil.rmtree(root / "opt/blitzecdn")

    result = _run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 1
    assert "Ansible is missing" in result.stderr
    assert any(path.exists() for path in owned if root / "opt/blitzecdn" not in path.parents)


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


def test_fresh_rebuild_keeps_a_2_x_checkout_on_the_release_branch(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    reinstall_marker = tmp_path / "fresh-reinstalled"
    clone_marker = tmp_path / "git-clone-arguments"
    _fake_installation(root)

    result = _run_sandboxed(
        script,
        "--fresh",
        "--yes",
        env_extra={
            "FRESH_GIT_TAG": "",
            "FRESH_GIT_BRANCH": "2.x",
            "FRESH_GIT_CLONE_MARKER": str(clone_marker),
            "FRESH_REINSTALL_MARKER": str(reinstall_marker),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert reinstall_marker.exists()
    clone_args = clone_marker.read_text(encoding="utf-8")
    assert "clone --branch 2.x" in clone_args
    assert "--depth" not in clone_args


def test_fresh_refuses_to_rebuild_without_a_git_checkout(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root, with_git=False)

    result = _run_sandboxed(script, "--fresh", "--yes")

    assert result.returncode == 1
    assert "is not a Git checkout" in result.stderr


# --- the `blitzecdn` command the no-argument install puts on PATH -------------
#
# The wrapper is what makes the CLI runnable outside the checkout, so these run
# the real generator against a fake HOME rather than asserting on its text.


def _install_user_wrapper(
    tmp_path: Path, checkout: Path, **env_extra: str
) -> subprocess.CompletedProcess[str]:
    """Run install_user_wrapper alone, with HOME redirected under tmp_path."""
    driver = tmp_path / "drive.sh"
    driver.write_text(
        "set -Eeuo pipefail\n"
        f"readonly USER_WRAPPER_MARKER={_marker()!r}\n"
        f'script_dir="{checkout}"\n'
        f"{_function_source('install_user_wrapper')}\n"
        "install_user_wrapper\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "HOME": str(tmp_path / "home"), **env_extra}
    return subprocess.run(  # noqa: S603 - fixed executable and generated driver
        [BASH, str(driver)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _marker() -> str:
    match = re.search(r"readonly USER_WRAPPER_MARKER='([^']*)'", _script())
    assert match is not None, "USER_WRAPPER_MARKER not found in install.sh"
    return match.group(1)


def _function_source(name: str) -> str:
    return f"{name}() {{{_function(name)}\n}}"


def test_user_wrapper_pins_the_checkout_as_the_project_directory(tmp_path: Path):
    """Without BLITZE_PROJECT_DIR the CLI would use the caller's directory."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    result = _install_user_wrapper(tmp_path, checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    wrapper = tmp_path / "home/.local/bin/blitzecdn"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    body = wrapper.read_text()
    assert f"BLITZE_PROJECT_DIR={checkout}" in body
    assert f"{checkout}/.venv/bin/blitzecdn" in body
    assert '"$@"' in body


def test_user_wrapper_is_rewritten_on_every_install(tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _install_user_wrapper(tmp_path, checkout)
    other = tmp_path / "other-checkout"
    other.mkdir()

    result = _install_user_wrapper(tmp_path, other)

    assert result.returncode == 0, result.stdout + result.stderr
    body = (tmp_path / "home/.local/bin/blitzecdn").read_text()
    assert f"BLITZE_PROJECT_DIR={other}" in body


def test_user_wrapper_never_overwrites_a_foreign_command(tmp_path: Path):
    """A `blitzecdn` this installer did not write may be anyone's program."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    wrapper = tmp_path / "home/.local/bin/blitzecdn"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")

    result = _install_user_wrapper(tmp_path, checkout)

    assert result.returncode == 0
    assert wrapper.read_text() == "#!/bin/sh\necho not ours\n"
    assert "was not written by this installer" in result.stderr


def test_user_wrapper_can_be_declined(tmp_path: Path):
    """The server installs suppress it; ${CLI_WRAPPER} is the command there."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    result = _install_user_wrapper(tmp_path, checkout, BLITZECDN_USER_WRAPPER="0")

    assert result.returncode == 0
    assert not (tmp_path / "home/.local/bin/blitzecdn").exists()


def test_user_wrapper_honours_an_explicit_directory(tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    target = tmp_path / "elsewhere/bin"

    result = _install_user_wrapper(
        tmp_path, checkout, BLITZECDN_WRAPPER_DIR=str(target)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "blitzecdn").exists()


def test_user_wrapper_survives_a_checkout_path_with_spaces(tmp_path: Path):
    checkout = tmp_path / "my checkout"
    checkout.mkdir()

    result = _install_user_wrapper(tmp_path, checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    wrapper = tmp_path / "home/.local/bin/blitzecdn"
    printed = subprocess.run(  # noqa: S603 - fixed executable, generated wrapper
        [BASH, "-c", f'set -- --version; . "{wrapper}"'],
        check=False,
        capture_output=True,
        text=True,
    )
    # The venv does not exist here; reaching an exec failure on the right path
    # proves the quoting survived the space.
    assert "my checkout/.venv/bin/blitzecdn" in printed.stderr


def test_the_default_install_ends_by_installing_the_wrapper():
    """Otherwise the CLI is only reachable from inside the checkout."""
    install = _section("install")
    assert "install_user_wrapper" in install


def test_private_copy_helper_copies_once_and_cleans_up_after_itself():
    """The mechanics the three destructive paths used to repeat verbatim."""
    helper = _function("reexec_from_private_copy")
    assert 'install -m 0700 -- "$0" "${copy}"' in helper
    assert 'exec env "${guard}=1" "${copy}" "${original_args[@]}"' in helper
    assert "trap 'rm -f -- \"$0\"' EXIT" in helper


# --- the control-plane role --------------------------------------------------
#
# Host state moved out of this script and into ansible/roles/blitzecdn_controlplane,
# so the properties that used to be asserted against bash are asserted against
# the role's tasks. What the role *does* is covered by running it on a real
# Debian/Ubuntu host; these pin the invariants that a reader cannot see from one
# successful run.

ROLE = PROJECT_DIR / "ansible/roles/blitzecdn_controlplane"


def _role_tasks() -> list[dict]:
    return yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))


def _role_task(name: str) -> dict:
    matches = [task for task in _role_tasks() if task.get("name") == name]
    assert len(matches) == 1, f"expected exactly one task named {name!r}"
    return matches[0]


def _evaluate_os_gate(distribution: str, major_version: str) -> bool:
    """Evaluate the role's supported-OS expression as Ansible would."""
    task = _role_task("Validate supported operating system")
    expression = task["ansible.builtin.assert"]["that"][0]
    environment = jinja2.Environment(  # noqa: S701 - evaluates a boolean, renders no markup
        undefined=jinja2.StrictUndefined
    )
    rendered = environment.from_string("{{ " + expression + " }}")
    facts = {
        "distribution": distribution,
        "distribution_major_version": major_version,
    }
    return rendered.render(ansible_facts=facts) == "True"


def test_role_never_rewrites_an_existing_environment_file():
    """Re-running the installer must not rotate an operator's API credential."""
    task = _role_task("Write the service environment")
    assert task["ansible.builtin.template"]["force"] is False
    # force alone is not enough: a template task still renders its source to
    # decide, and the credential only exists as a fact on a first run.
    assert task["when"] == "not blitzecdn_controlplane_environment.stat.exists"
    assert task["no_log"] is True


def test_host_key_scan_is_stable_across_runs():
    """`ssh-keyscan -H` salts each run, so every converge would report a change."""
    task = _role_task("Scan the loopback host keys")
    assert "-H" not in task["ansible.builtin.command"]["argv"]
    written = _role_task("Record the loopback host keys")
    assert "sort" in written["ansible.builtin.copy"]["content"]


def test_controlplane_role_services_are_managed_as_units():
    """Every service started by the role must be one of its installed units."""
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    for service in defaults["blitzecdn_controlplane_services"]:
        assert service in defaults["blitzecdn_controlplane_units"]


def test_standalone_bootstraps_only_what_ansible_needs():
    """Everything else is the role's job; this list is the bootstrap contract."""
    standalone = _section("standalone")
    # uv creates the virtualenv itself, so python3-venv left the list; curl and
    # ca-certificates joined it because the pinned uv is fetched over HTTPS.
    assert "apt-get install -y ca-certificates curl git python3" in standalone
    assert "converge_control_plane" in standalone
    # Accounts, sudo, SSH trust and units must not be done twice.
    for moved in ("useradd", "visudo", "ssh-keygen", "ssh-keyscan", "openssl rand"):
        assert moved not in standalone, f"{moved} still runs in the installer"


def test_uninstall_succeeds_after_ansible_teardown(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root)

    result = _run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BlitzeCDN has been removed" in result.stdout


def test_the_controller_floor_is_higher_than_the_edge_floor():
    """One host is both under `standalone`, and they do not have the same needs.

    The edge playbook accepts Debian 12; the control-plane role must not, because
    Debian 12 ships Python 3.11. Sharing one gate would either lock working edges
    out or let an unusable controller through.
    """
    edge = yaml.safe_load(
        (PROJECT_DIR / "ansible/playbooks/edge.yml").read_text(encoding="utf-8")
    )
    edge_gate = edge[0]["pre_tasks"][0]["ansible.builtin.assert"]["that"][0]
    assert ">= 12" in edge_gate, "edges must keep accepting Debian 12"

    controller_gate = _role_task("Validate supported operating system")
    expression = controller_gate["ansible.builtin.assert"]["that"][0]
    assert ">= 13" in expression, "the controller needs a Python 3.12+ platform"


def test_no_upper_bound_on_the_python_version():
    """A ceiling is discovered on somebody's fresh server, never in CI.

    An interpreter that genuinely breaks the control plane should fail the test
    suite, not be refused at install time on a host nobody has tried yet.
    """
    script = (PROJECT_DIR / "install.sh").read_text(encoding="utf-8")
    assert "sys.version_info[:2] < (3, 12)" in script
    assert "(3, 15)" not in script, "install.sh still refuses a future Python"

    pyproject = (PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.12"' in pyproject


# --- uv: the pinned toolchain the installer builds the virtualenv with --------

#: Preamble for a stub curl: find the output path the way curl does.
#:
#: install.sh passes several flags before `-o`, so a stub that guesses at a
#: positional argument writes the download somewhere else entirely — and then
#: the checksum test still "passes", because a file that was never created
#: cannot match either. Parsing the flag is what makes these tests mean
#: something.
_CURL_STUB = """#!/usr/bin/env bash
out=""
while [[ $# -gt 0 ]]; do
  if [[ $1 == -o ]]; then out=$2; shift 2; else shift; fi
done
[[ -n "${out}" ]] || { echo "stub curl: no -o argument" >&2; exit 2; }
"""


def _host_uv_target() -> str:
    """The release triple `ensure_uv` should pick on the machine running this.

    Derived rather than hardcoded: these tests run on the Linux servers CI uses
    and on the macOS laptops the controller-only checkout is developed on, and
    a harness that always expected a Linux triple is exactly the assumption
    that shipped a Linux binary to a Mac.
    """
    platforms = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}
    architectures = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    system = platforms[platform.system()]
    return f"{architectures[platform.machine()]}-{system}"


def _uv_harness(tmp_path: Path, *, curl_body: str, digest: str = "0" * 64) -> Path:
    """A runnable script holding just `die` and `ensure_uv`.

    Extracted from install.sh rather than copied, so a change to either
    function is exercised here instead of drifting away from a stale duplicate.
    The script cannot be sourced whole — its last line dispatches a subcommand.
    """
    pins = re.search(
        r"^UV_VERSION=.*?^UV_SHA256_aarch64_apple_darwin=.*?$",
        _script(),
        re.DOTALL | re.MULTILINE,
    )
    assert pins is not None, "the uv pins are no longer where the harness expects"
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        f"{pins.group(0)}\n"
        "die() {\n" + _function("die") + "\n}\n"
        "sha256_of() {\n" + _function("sha256_of") + "\n}\n"
        "ensure_uv() {\n" + _function("ensure_uv") + "\n}\n"
        "ensure_uv\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "curl").write_text(curl_body, encoding="utf-8")
    (stub_bin / "curl").chmod(0o755)
    # install.sh calls sha256sum, which is right for the Debian and Ubuntu
    # servers it installs onto but absent on a macOS developer machine. Stubbed
    # so the test runs the same everywhere: what is under test is the
    # comparison and the refusal, not the hashing.
    (stub_bin / "sha256sum").write_text(
        f'#!/usr/bin/env bash\nprintf "%s  %s\\n" "{digest}" "$1"\n',
        encoding="utf-8",
    )
    (stub_bin / "sha256sum").chmod(0o755)
    return harness


def _run_harness(tmp_path: Path, harness: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and generated script
        [BASH, str(harness)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        # Deliberately not the caller's PATH: a developer with uv installed
        # would otherwise satisfy the lookup and skip the code under test.
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_the_installer_pins_uv_and_a_checksum_for_every_architecture():
    """An unpinned download is an unreviewed dependency running as root."""
    script = _script()
    assert re.search(r'^UV_VERSION="\d+\.\d+\.\d+"$', script, re.MULTILINE)
    for triple in (
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
    ):
        name = "UV_SHA256_" + triple.replace("-", "_")
        assert re.search(rf'^{name}="[0-9a-f]{{64}}"$', script, re.MULTILINE), (
            f"no pinned checksum for {triple}"
        )
    # Downloaded over HTTPS with the protocol pinned, so a redirect to plain
    # HTTP cannot silently downgrade the fetch.
    assert "--proto '=https' --tlsv1.2" in script


def test_a_uv_download_that_fails_its_checksum_is_refused(tmp_path: Path):
    """The whole point of pinning: a tampered archive must never be executed."""
    harness = _uv_harness(
        tmp_path,
        # Writes a well-formed file that is simply not the pinned artifact.
        curl_body=_CURL_STUB + 'printf "not the real uv" > "${out}"\n',
    )

    result = _run_harness(tmp_path, harness)

    assert result.returncode != 0
    assert "does not match its pinned checksum" in result.stderr
    assert not (tmp_path / ".state/bin/uv").exists(), "refused, but installed anyway"


@pytest.mark.parametrize(
    ("system", "machine", "triple"),
    [
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Darwin", "arm64", "aarch64-apple-darwin"),
    ],
)
def test_the_download_follows_the_host_os_not_only_its_architecture(
    tmp_path: Path, system: str, machine: str, triple: str
):
    """A Linux build unpacked on a Mac passes its checksum and then cannot run.

    The failure surfaces as `cannot execute binary file` well after the point
    that was supposed to catch a wrong artifact, so the triple has to be right
    before the download, not discovered after it.
    """
    harness = _uv_harness(
        tmp_path,
        # Records the URL, then writes something that fails the checksum: what
        # is under test is which artifact was asked for.
        curl_body=(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "${HOME}/requested-url"\n'
            + _CURL_STUB
            + 'printf "not the real uv" > "${out}"\n'
        ),
    )
    uname = tmp_path / "bin" / "uname"
    uname.write_text(
        "#!/usr/bin/env bash\n"
        f'case "$1" in -s) echo {system} ;; -m) echo {machine} ;; esac\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)

    result = _run_harness(tmp_path, harness)

    assert result.returncode != 0, "the stub archive should have failed its checksum"
    assert f"uv-{triple}.tar.gz" in (tmp_path / "requested-url").read_text()


def test_an_unsupported_os_says_so_instead_of_downloading_a_linux_build(
    tmp_path: Path,
):
    """Naming the platform is the difference between a fix and a puzzle."""
    harness = _uv_harness(
        tmp_path,
        curl_body='#!/usr/bin/env bash\necho "curl must not run" >&2\nexit 1\n',
    )
    uname = tmp_path / "bin" / "uname"
    uname.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in -s) echo FreeBSD ;; -m) echo x86_64 ;; esac\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)

    result = _run_harness(tmp_path, harness)

    assert result.returncode != 0
    assert "no pinned uv build for FreeBSD" in result.stderr


def test_an_existing_recent_uv_is_used_rather_than_downloaded(tmp_path: Path):
    """A host that already manages uv must not grow a second copy."""
    harness = _uv_harness(
        tmp_path,
        curl_body='#!/usr/bin/env bash\necho "curl must not run" >&2\nexit 1\n',
    )
    stub_uv = tmp_path / "bin" / "uv"
    # Far above any version this installer will pin.
    stub_uv.write_text('#!/usr/bin/env bash\necho "uv 99.0.0"\n', encoding="utf-8")
    stub_uv.chmod(0o755)

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub_uv)
    assert not (tmp_path / ".state/bin/uv").exists()


def test_a_verified_uv_download_returns_only_its_path(tmp_path: Path):
    """The function's stdout is its return value, so nothing else may go there.

    A progress message printed to stdout is substituted into the caller's
    `uv="$(ensure_uv)"` along with the path, and the install then fails on a
    command name with a sentence in front of it.
    """
    target = _host_uv_target()
    name = "UV_SHA256_" + target.replace("-", "_")
    expected = re.search(rf'^{name}="([0-9a-f]{{64}})"$', _script(), re.MULTILINE)
    assert expected is not None
    harness = _uv_harness(
        tmp_path,
        # A real tar laid out the way the release is: one directory named for
        # the target, holding the binary ensure_uv strips a component off.
        curl_body=(
            _CURL_STUB + 'work="$(mktemp -d)"\n'
            f'mkdir -p "${{work}}/uv-{target}"\n'
            'printf "#!/bin/sh\\necho uv 0.0.0\\n" '
            f'> "${{work}}/uv-{target}/uv"\n'
            f'tar -czf "${{out}}" -C "${{work}}" uv-{target}\n'
        ),
        digest=expected.group(1),
    )

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / ".state/bin/uv")
    assert (tmp_path / ".state/bin/uv").is_file()


def test_cli_wrapper_preserves_the_explicit_empty_site_approval():
    """The one destructive approval must survive sudo and the env file."""
    wrapper = (
        PROJECT_DIR / "ansible/roles/blitzecdn_controlplane/templates/blitzecdn-cli.j2"
    ).read_text(encoding="utf-8")

    captured = "allow_empty_sites_override=${BLITZE_ALLOW_EMPTY_SITES-}"
    restored = "BLITZE_ALLOW_EMPTY_SITES=${allow_empty_sites_override}"
    assert captured in wrapper
    assert "--preserve-env=BLITZE_ALLOW_EMPTY_SITES" in wrapper
    assert restored in wrapper
    assert wrapper.index(captured) < wrapper.index("source ") < wrapper.index(restored)
