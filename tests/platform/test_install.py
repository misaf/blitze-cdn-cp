"""Behavioral tests for install.sh.

The privileged subcommands refuse to run as a normal user, so the paths that
provision a server cannot be executed directly here. What *can* be executed is
everything that decides whether to provision: dispatch, argument parsing, and
the validators the script shells out to Python for. Those run directly against
the source extracted from the script, so the assertions describe behavior
rather than the text that happens to implement it.

The lifecycle paths (``--uninstall``, ``--fresh`` and ``update``) are exercised
for real in a sandbox: the script is copied with every path it touches
redirected under a temp directory and the root check neutralised, and the
privileged commands (systemctl, userdel, nginx, git) are stubbed on
``PATH``. That lets a test verify what is actually removed and that the
reinstall takes the same path as a brand-new server, without touching the host.

``update`` is sandboxed only as far as its point of no return: past that it
rebuilds the virtualenv, which downloads uv and resolves a lockfile. Each of
its tests drives the run into a documented refusal and asserts the host was
left serving.

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
from paths import CORE_ANSIBLE, REPO_ROOT

PROJECT_DIR = REPO_ROOT
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


def _query(expression: str) -> str:
    """Source install.sh and evaluate one expression against its functions.

    The script guards its dispatch on ``BASH_SOURCE[0] == $0``, so sourcing it
    defines everything and provisions nothing. That is what lets a pure helper
    be tested by calling it, instead of by asserting that some literal string
    appears in the file — assertions of that shape broke on every refactor that
    changed no behaviour at all.
    """
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, "-c", f'source "{SCRIPT}"\n{expression}'],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{expression!r} failed: {result.stderr.strip()}"
    return result.stdout.strip()


def _section(name: str) -> str:
    """Return one subcommand's body.

    Assertions about what a subcommand must never do have to be scoped to it:
    the three installers share a file now, and `standalone` legitimately runs a
    deployment that destructive lifecycle commands must never run.
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
    # USERDEL_REFUSES makes the deployment-account removal fail as it would for
    # an account that still has a process running.
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
        '    [[ -z "${FRESH_GIT_CLONE_FAIL:-}" ]] || exit 1\n'
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
        root
        / "opt/blitzecdn/.state/certificates/example-cdn"
        / "fullchain-deadbeef.pem",
        root
        / "opt/blitzecdn/.state/collections/ansible_collections/blitzecdn"
        / "edge/MANIFEST.json",
        root / "opt/blitzecdn/.state/letsencrypt/config",
        root / "opt/blitzecdn/.state/ansible-local",
        root / "opt/blitzecdn/.venv/bin/python",
        root / "opt/blitzecdn/.venv/bin/ansible-playbook",
        root / "opt/blitzecdn/src/blitzecdn/ansible/playbooks/uninstall.yml",
        root / "opt/blitzecdn/log/run.log",
        root / "etc/blitzecdn/firewall-rules",
        root / "etc/blitzecdn/control-plane.compose.yml",
        root / "etc/systemd/system/blitzecdn-geoipupdate.service",
        root / "etc/systemd/system/blitzecdn-geoipupdate.timer",
        root / "usr/local/bin/blitzecdn",
        root / "etc/sudoers.d/blitzecdn-deploy",
        root / "etc/nginx/blitzecdn-managed-sites",
        root / "etc/nginx/conf.d/blitzecdn-plugin-cache-http.conf",
        root / "etc/nginx/conf.d/blitzecdn-plugin-geoip-http.conf",
        root / "etc/nginx/conf.d/blitzecdn-status.conf",
        root / "etc/nginx/sites-available/example-cdn.conf",
        root / "etc/nginx/sites-enabled/example-cdn.conf",
        root / "var/cache/nginx/blitzecdn/cache-data",
        root / "etc/systemd/resolved.conf.d/blitzecdn.conf",
        root / "var/log/nginx/blitzecdn-access.log",
        root / "etc/fail2ban/jail.d/blitzecdn-sshd.local",
        root / "etc/ssh/sshd_config.d/50-blitzecdn.conf",
        root / "etc/sysctl.d/60-blitzecdn.conf",
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
    system_paths = [
        path for path in owned if root / "opt/blitzecdn" not in path.parents
    ]
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
        root / "var/backups/blitzecdn/operator-backup.db",
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
    bin_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    # `update` refuses to run from anywhere but the installation directory, so
    # its copy of the script sits there rather than beside the stubs.
    environment = os.environ.copy()
    stubs = bin_dir if bin_dir is not None else script.parent / "bin"
    environment["PATH"] = f"{stubs}:{environment['PATH']}"
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
    """Preparing a server and deploying to it are separate decisions."""
    required = "--admin-cidr 203.0.113.8/32 --email operator@example.com"
    assert (
        _query(
            f"parse_options standalone usage_standalone {required}\n"
            'echo "${parsed_deploy}"'
        )
        == "0"
    )
    assert (
        _query(
            f"parse_options standalone usage_standalone {required} --deploy\n"
            'echo "${parsed_deploy}"'
        )
        == "1"
    )
    # Forwarding happens inside a root-only command, so it stays structural.
    assert "handoff_args+=(--deploy)" in _section("standalone")


def test_standalone_guards_existing_sites_from_empty_desired_state():
    standalone = _section("standalone")
    assert 'BLITZE_ALLOW_EMPTY_SITES="${parsed_allow_empty_sites}"' in standalone
    assert "blitzecdn_nginx_allow_empty_sites" in (
        PROJECT_DIR / "src/blitzecdn/capabilities/deployments/desired_state.py"
    ).read_text(encoding="utf-8")


def test_role_keeps_the_installation_tree_root_owned_and_read_only_to_runtime():
    installation = _role_task("Set the installation directory mode")
    arguments = installation["ansible.builtin.file"]
    assert arguments["owner"] == "root"
    assert arguments["group"] == "root"
    assert arguments["mode"] == "0755"
    assert arguments.get("recurse") is not True


def test_installer_installs_only_third_party_collections():
    """The BlitzeCDN roles ship inside the wheel; nothing pins or builds them.

    The --force that used to be here worked around ansible-core comparing a
    v-prefixed Git ref to a numeric manifest. With no Git-backed collection
    left, needing it again would mean the roles had been re-externalised.
    """
    script = _script()
    assert (
        '-r src/blitzecdn/ansible/requirements.yml -p "${collections_path}"' in script
    )
    assert "--force" not in script
    assert "collection build" not in script


def test_production_bootstrap_does_not_install_the_application_on_the_host():
    script = _script()
    assert "bootstrap_runtime ansible-only" in _section("standalone")
    assert "bootstrap_runtime ansible-only" in _section("update")
    assert "--no-dev --no-install-project" in script


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


def test_uninstall_reuses_the_canonical_edge_teardown_role():
    uninstall_tasks = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_uninstall/tasks/main.yml").read_text(
            encoding="utf-8"
        )
    )
    handoffs = [
        task
        for task in uninstall_tasks
        if task.get("ansible.builtin.include_role", {}).get("name")
        == "blitzecdn_teardown"
    ]
    assert len(handoffs) == 1
    assert handoffs[0]["vars"]["blitzecdn_teardown_remove_logs"] is True

    serialized = yaml.safe_dump(uninstall_tasks)
    for duplicated_edge_detail in (
        "nginx_marker",
        "sites-available",
        "sites-enabled",
        "blitzecdn-managed-sites",
        "/var/cache/nginx",
        "/var/log/nginx",
    ):
        assert duplicated_edge_detail not in serialized


def test_destructive_commands_require_confirmation():
    for name in ("uninstall", "fresh"):
        section = _section(name)
        assert "confirm_destructive" in section
        assert 'confirm_destructive "${parsed_yes}"' in section
    parser = _function("parse_options")
    # The flag is handled once, in the shared parser, rather than by each
    # command reading its own arguments. Matched loosely: the case label grows
    # a branch whenever a command gains a flag, and that is not what this
    # guards.
    assert re.search(r"^      --yes\|.*\)$", parser, re.MULTILINE)
    # And it is the parser that records it, for every command that takes it.
    for command in ("uninstall", "fresh", "update"):
        usage = _query(f"command_field {command} 2")
        assert (
            _query(f'parse_options {command} {usage} --yes\necho "${{parsed_yes}}"')
            == "1"
        )
        assert _query(f'parse_options {command} {usage}\necho "${{parsed_yes}}"') == "0"
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
    assert '[[ ${revision} != "3.x" ]]' in fresh
    assert 'git clone --branch "${revision}"' in fresh
    assert "git clone --depth 1" not in fresh
    assert 'git -C "${staging}" checkout --detach "${revision}"' in fresh
    assert '"${INSTALL_DIR}/install.sh" standalone ' in fresh
    assert '${parsed_forward_args[@]+"${parsed_forward_args[@]}"}' in fresh


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
    assert any(
        path.exists() for path in owned if root / "opt/blitzecdn" not in path.parents
    )


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


def test_fresh_rebuild_keeps_a_3_x_checkout_on_the_release_branch(tmp_path: Path):
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
            "FRESH_GIT_BRANCH": "3.x",
            "FRESH_GIT_CLONE_MARKER": str(clone_marker),
            "FRESH_REINSTALL_MARKER": str(reinstall_marker),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert reinstall_marker.exists()
    clone_args = clone_marker.read_text(encoding="utf-8")
    assert "clone --branch 3.x" in clone_args
    assert "--depth" not in clone_args


def test_fresh_refuses_to_rebuild_without_a_git_checkout(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root, with_git=False)

    result = _run_sandboxed(script, "--fresh", "--yes")

    assert result.returncode == 1
    assert "is not a Git checkout" in result.stderr


def test_fresh_clone_failure_preserves_the_running_installation(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    owned = _fake_installation(root)

    result = _run_sandboxed(
        script,
        "--fresh",
        "--yes",
        env_extra={"FRESH_GIT_CLONE_FAIL": "1"},
    )

    assert result.returncode == 1
    assert "current installation was not changed" in result.stderr
    assert all(path.exists() for path in owned)


# --- update: structural guarantees -------------------------------------------


def test_update_preserves_state_rather_than_removing_it():
    """The whole point of the command: --fresh destroys, update does not."""
    update = _section("update")
    for destructive in (
        "converge_uninstall",
        "remove_installation_directory",
        "reexec_from_private_copy",
        "userdel",
    ):
        assert destructive not in update, f"update must never {destructive}"


def test_update_takes_a_backup_before_it_changes_anything():
    update = _section("update")
    assert "backup create --only database" in update
    # Ordering is the guarantee. A backup taken after the checkout, or after the
    # migration, is a backup of the thing it was supposed to protect against.
    marker = "backup create --only database"
    assert update.index(marker) < update.index("stop_control_plane_services")
    assert update.index(marker) < update.index("bootstrap_runtime")


def test_update_stops_the_services_before_migrating_the_schema():
    """The converge runs `blitzecdn setup --schema-only` against a live host."""
    update = _section("update")
    assert update.index("stop_control_plane_services") < update.index(
        "converge_control_plane"
    )
    assert update.index("bootstrap_runtime") < update.index("converge_control_plane")


def test_update_only_ever_moves_forward_onto_a_release_tag():
    """A merge or a rewrite on a server is never what an operator meant.

    The forward-only question is now asked directly, rather than inferred from
    a fast-forward merge failing, so the assertion names the check itself.
    """
    update = _section("update")
    assert 'repo_git merge-base --is-ancestor HEAD "${target}^{commit}"' in update
    assert 'repo_git checkout --quiet "${target}"' in update
    # `merge --ff-only`, not a bare "merge": `merge-base` contains that word.
    forbidden = ("git pull", "merge --ff-only", "--force", "reset --hard", "rebase")
    for operation in forbidden:
        assert operation not in update


def test_update_never_crosses_a_major_line():
    """The one property that keeps an unattended updater from a major upgrade."""
    resolve = _function("latest_release_tag")
    assert 'repo_git tag --list "v${major}.*" --sort=-v:refname' in resolve
    # A pre-release or a branch-shaped tag is not an installable release.
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in resolve
    assert 'latest_release_tag "${major}"' in _section("update")


def test_update_decides_where_it_is_going_before_it_stops_anything():
    """Every refusal about *which* release must cost no downtime."""
    update = _section("update")
    assert update.index("latest_release_tag") < update.index(
        "backup create --only database"
    )
    assert update.index("merge-base --is-ancestor") < update.index(
        "stop_control_plane_services"
    )


def test_update_verifies_the_code_it_is_about_to_run_as_root():
    update = _section("update")
    assert "require_upstream_origin" in update
    assert "status --porcelain" in update
    assert "${INSTALL_DIR} is not a Git checkout" in update


def test_update_stops_both_persistent_compose_services():
    declared = re.search(
        r"readonly CONTROL_PLANE_SERVICES=\((.*?)\)", _script(), re.DOTALL
    )
    assert declared is not None, "install.sh no longer declares CONTROL_PLANE_SERVICES"
    assert declared.group(1).split() == ["blitzecdn-api", "blitzecdn-worker"]


# --- update: sandboxed behaviour ---------------------------------------------
#
# Everything up to the point of no return runs for real here. Past it the
# command rebuilds the virtualenv, which downloads uv and resolves a lockfile —
# too heavy for a unit test and covered instead by the container lifecycle.
# So each test below drives the run into a documented refusal and asserts the
# host was left alone.


def _stub_update_git(sandbox: Path) -> None:
    """Answer the questions cmd_update asks, driven by environment variables.

    Written over the fresh-rebuild stub because the two commands ask disjoint
    questions and a single stub answering both would be harder to read than
    either.

    The arm order is load-bearing, and every trap here fails as a silently
    wrong answer rather than an error:

    * `*fetch*` stays above the tag arm, and the tag arm matches
      ``tag --list`` rather than ``tag``, because the real call is
      ``fetch --tags --prune origin``.
    * ``--exact-match`` precedes ``--abbrev=0``: both contain
      ``describe --tags``, so a looser arm above them would pin
      `describe_installed_release` to its first branch forever.
    * Two different ``rev-list --count`` questions exist now — the commits
      ahead of the target, and the drift past the nearest tag — so the
      ``HEAD..`` form has to be matched before the general one.
    * Patterns are substring matches with a leading ``*`` because ``$*``
      carries `repo_git`'s ``-c safe.directory=... -C <path>`` prefix.
    * The unset-only default form (``${VAR-x}``, not ``${VAR:-x}``) is what
      lets a test pass ``""`` to mean "this question has no answer".
    """
    (sandbox / "bin" / "git").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"status --porcelain"*)\n'
        '    printf "%s" "${UPDATE_GIT_DIRTY:-}"; exit 0 ;;\n'
        '  *"remote get-url origin"*)\n'
        '    echo "https://github.com/misaf/blitze-cdn-cp.git"; exit 0 ;;\n'
        "  *fetch*)\n"
        '    exit "${UPDATE_GIT_FETCH_STATUS:-0}" ;;\n'
        '  *"show HEAD:pyproject.toml"*)\n'
        '    [[ -n "${UPDATE_PROJECT_VERSION-3.0.0}" ]] || exit 0\n'
        '    printf \'version = "%s"\\n\' "${UPDATE_PROJECT_VERSION-3.0.0}"\n'
        "    exit 0 ;;\n"
        '  *"tag --list"*)\n'
        # %b, not %s: the default carries escaped newlines that must become
        # real ones, or the whole list arrives as one unparsable line.
        '    printf "%b" "${UPDATE_GIT_TAGS-v3.1.0\\nv3.0.0\\n}"; exit 0 ;;\n'
        '  *"merge-base --is-ancestor"*)\n'
        '    exit "${UPDATE_GIT_ANCESTOR_STATUS:-0}" ;;\n'
        '  *"describe --tags --exact-match HEAD"*)\n'
        '    [[ -n "${UPDATE_GIT_EXACT_TAG-v3.0.0}" ]] || exit 1\n'
        '    echo "${UPDATE_GIT_EXACT_TAG-v3.0.0}"; exit 0 ;;\n'
        '  *"describe --tags --abbrev=0"*)\n'
        '    [[ -n "${UPDATE_GIT_NEAREST_TAG-v3.0.0}" ]] || exit 1\n'
        '    echo "${UPDATE_GIT_NEAREST_TAG-v3.0.0}"; exit 0 ;;\n'
        '  *"rev-list --count HEAD.."*)\n'
        '    echo "${UPDATE_GIT_COMMITS:-12}"; exit 0 ;;\n'
        '  *"rev-list --count"*)\n'
        '    echo "${UPDATE_GIT_DRIFT:-4}"; exit 0 ;;\n'
        '  *"rev-parse --short HEAD"*)\n'
        '    echo "${UPDATE_GIT_SHORT_SHA:-abc1234}"; exit 0 ;;\n'
        "  *checkout*)\n"
        '    if [[ -n "${UPDATE_GIT_CHECKOUT_FAILS:-}" ]]; then exit 1; fi\n'
        '    printf "%s\\n" "checkout" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        "    exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (sandbox / "bin" / "git").chmod(0o700)


def _stub_update_services(sandbox: Path, root: Path) -> None:
    """Docker Compose and the CLI wrapper, both recording into the order log."""
    (sandbox / "bin" / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *" stop blitzecdn-api blitzecdn-worker"* ]]; then\n'
        '  printf "%s\\n" "stop blitzecdn-api blitzecdn-worker" '
        '>> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (sandbox / "bin" / "docker").chmod(0o700)
    wrapper = root / "usr/local/bin/blitzecdn"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "backup" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        'exit "${UPDATE_BACKUP_STATUS:-0}"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)


def _update_sandbox(
    tmp_path: Path, *, with_git: bool = True
) -> tuple[Path, Path, Path]:
    """Build an installation whose install.sh *is* the instrumented script.

    `update` requires that: it refuses to run from anywhere but the
    installation directory, because the checkout it updates is the one it is
    running from. Returns the script to run, the stub directory, and the root.
    """
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _stub_update_git(sandbox)
    _fake_installation(root, with_git=with_git)
    _stub_update_services(sandbox, root)
    installed = root / "opt/blitzecdn/install.sh"
    shutil.copyfile(script, installed)
    installed.chmod(0o700)
    return installed, sandbox / "bin", root


def test_update_refuses_a_checkout_with_local_modifications(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_GIT_DIRTY": " M install.sh\n"},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "local modifications" in result.stderr


def test_update_refuses_without_a_git_checkout(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path, with_git=False)

    result = _run_sandboxed(script, "update", "--yes", bin_dir=stubs)

    assert result.returncode == 1
    assert "is not a Git checkout" in result.stderr


def test_update_leaves_the_host_alone_when_the_fetch_fails(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_GIT_FETCH_STATUS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "could not fetch" in result.stderr
    assert not log.exists(), "a failed fetch stopped no services and took no backup"


def test_update_moves_a_detached_release_onto_the_newest_tag_in_its_line(
    tmp_path: Path,
):
    """The ordinary case: a release install follows its line without being told.

    Driven into the checkout failure so the run stops before the rebuild, which
    would download uv and resolve a lockfile.
    """
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "v3.0.0",
            "UPDATE_GIT_TAGS": "v3.1.0\nv3.0.0\n",
            "UPDATE_GIT_CHECKOUT_FAILS": "1",
        },
        bin_dir=stubs,
    )

    assert "Checking out v3.1.0" in result.stdout
    assert result.returncode == 1
    assert "the containers are stopped" in result.stderr, (
        "past the point of no return, the refusal must say how to recover"
    )
    assert "checkout v3.0.0" in result.stderr, "and name the release to restore"


def test_update_ignores_releases_from_another_major_line(tmp_path: Path):
    """A 4.0.0 release is a migration an operator opts into, not an update."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_PROJECT_VERSION": "3.0.0",
            # What `tag --list "v3.*"` would return: the v4 line is not offered
            # to a v3 host at all, so an empty answer is a v3-only repository.
            "UPDATE_GIT_TAGS": "",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "no v3.x release tag" in result.stderr
    assert "nothing was changed" in result.stderr
    assert not log.exists()


def test_update_refuses_a_checkout_that_is_not_an_ancestor_of_the_release(
    tmp_path: Path,
):
    """Local commits, or a host already past the tag: --fresh is the tool."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_GIT_ANCESTOR_STATUS": "1",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "is not an ancestor of v3.1.0" in result.stderr
    assert "--fresh" in result.stderr
    assert "nothing was changed" in result.stderr
    assert not log.exists(), "a host that could not be updated was still taken down"


def test_update_refuses_when_the_project_version_is_unreadable(tmp_path: Path):
    """Without a version there is no release line, so there is no target."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_PROJECT_VERSION": "", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "nothing was changed" in result.stderr
    assert not log.exists()


def test_update_is_a_no_op_when_already_on_the_newest_release(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "v3.1.0",
            "UPDATE_GIT_TAGS": "v3.1.0\n",
            "UPDATE_ORDER_LOG": str(log),
        },
        bin_dir=stubs,
    )

    assert result.returncode == 0
    assert "Already on v3.1.0" in result.stdout
    assert "nothing to update" in result.stdout
    assert not log.exists(), "an up-to-date host was still taken down"


def test_update_asks_for_confirmation_and_can_be_cancelled(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        input="n\n",
        env_extra={"UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 0
    assert "Cancelled" in result.stdout
    assert "v3.0.0" in result.stderr, "the prompt must name the release it leaves"
    assert "v3.1.0" in result.stderr, "and the one it moves to"
    assert "(12 commits)" in result.stderr, "and how far apart they are"
    assert "-g" not in result.stderr, (
        "an operator is shown release versions, never a describe suffix"
    )
    assert not log.exists()


def test_update_names_the_drifted_release_a_branch_checkout_is_leaving(
    tmp_path: Path,
):
    """A host past its tag says so, rather than claiming to be on that release."""
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        input="n\n",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "",
            "UPDATE_GIT_NEAREST_TAG": "v3.0.0",
            "UPDATE_GIT_DRIFT": "4",
        },
        bin_dir=stubs,
    )

    assert "v3.0.0 (+4 commits)" in result.stderr


def test_update_falls_back_to_the_recorded_version_when_no_tag_is_reachable(
    tmp_path: Path,
):
    script, stubs, _ = _update_sandbox(tmp_path)

    result = _run_sandboxed(
        script,
        "update",
        input="n\n",
        env_extra={
            "UPDATE_GIT_EXACT_TAG": "",
            "UPDATE_GIT_NEAREST_TAG": "",
            "UPDATE_GIT_SHORT_SHA": "abc1234",
        },
        bin_dir=stubs,
    )

    assert "v3.0.0 (abc1234)" in result.stderr


def test_update_stops_nothing_when_the_backup_fails(tmp_path: Path):
    """A failed backup must abort while the controller is still serving."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_BACKUP_STATUS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    assert "backup failed" in result.stderr
    assert log.read_text(encoding="utf-8").split() == ["backup"]


def test_update_backs_up_and_stops_services_in_that_order(tmp_path: Path):
    """Driven into the checkout failure so it stops before the heavy rebuild."""
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        env_extra={"UPDATE_GIT_CHECKOUT_FAILS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert result.returncode == 1
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded[0] == "backup"
    assert "stop blitzecdn-api blitzecdn-worker" in recorded
    assert "checkout" not in recorded


def test_update_can_skip_the_backup_but_says_so(tmp_path: Path):
    script, stubs, _ = _update_sandbox(tmp_path)
    log = tmp_path / "order.log"

    result = _run_sandboxed(
        script,
        "update",
        "--yes",
        "--no-backup",
        env_extra={"UPDATE_GIT_CHECKOUT_FAILS": "1", "UPDATE_ORDER_LOG": str(log)},
        bin_dir=stubs,
    )

    assert "Skipping the database backup" in result.stdout
    assert "backup" not in log.read_text(encoding="utf-8").splitlines()


def test_the_default_install_ends_by_installing_the_wrapper():
    """Otherwise the CLI is only reachable from inside the checkout."""
    install = _section("install")
    assert "handoff_user_wrapper" in install


def test_every_command_uses_the_shared_option_parser():
    """One parser, and one table that routes every command through it.

    Asserted against the sourced script rather than its text: the command table
    is the declaration now, so the property is that it has a row per command,
    not that the file contains five dispatch lines.
    """
    assert _script().count("while [[ $# -gt 0 ]]") == 1
    for command in ("install", "standalone", "uninstall", "fresh", "update"):
        assert _query(f"command_field {command} 2") != "", (
            f"{command} has no row in COMMAND_TABLE"
        )


def test_both_capability_renderings_come_from_one_list():
    """The uv extras and the Ansible list must name the same capabilities.

    They are consumed by different things — one builds the virtualenv, the
    other tells the role which capability configuration to write — and a
    controller given configuration for a capability it does not have refuses to
    start. Each was expanded by hand before, so drift was a live possibility.
    """
    for override in ("", "backup cache"):
        prelude = f'export BLITZECDN_CAPABILITIES="{override}"\n' if override else ""
        extras = _query(f"{prelude}capability_extras").split()
        rendered = _query(f"{prelude}capability_json")

        # "--extra name --extra name ..." -> the names alone.
        assert extras[::2] == ["--extra"] * (len(extras) // 2)
        from_extras = extras[1::2]
        from_json = [name.strip('"') for name in rendered.split(",")]

        assert from_extras == from_json
        if override:
            assert from_extras == override.split()


def test_the_command_table_and_the_help_agree_on_every_option():
    """The table is what accepts an option; the help is what advertises it.

    They were separate lists and could disagree — an option the parser took but
    no help mentioned, or the reverse. Neither is reachable now without this
    failing.
    """
    # The two whole-host operations are spelled as flags on the command line,
    # so the row name and the token an operator types are not the same word.
    invocations = {
        "standalone": "standalone",
        "update": "update",
        "uninstall": "--uninstall",
        "fresh": "--fresh",
    }
    for command, token in invocations.items():
        declared = _query(f"command_field {command} 4").split()
        assert declared, f"{command} declares no options"
        helped = _run(token, "--help").stdout
        for option in declared:
            assert option in helped, f"{command} accepts {option} but never says so"


def test_private_copy_helper_copies_once_and_cleans_up_after_itself():
    """The mechanics the three destructive paths used to repeat verbatim."""
    helper = _function("reexec_from_private_copy")
    assert 'install -m 0700 -- "$0" "${copy}"' in helper
    assert 'exec env "${guard}=1" "${copy}" "${original_args[@]}"' in helper
    # The copy deletes itself on the way out. It registers with the shared
    # cleanup stack rather than setting its own EXIT trap, which would replace
    # the handler and strand everything else the run had asked to clean up.
    assert 'cleanup_paths+=("$0")' in helper
    code = [line for line in helper.splitlines() if not line.lstrip().startswith("#")]
    assert not any(line.lstrip().startswith("trap ") for line in code), (
        "a second EXIT trap here replaces the shared cleanup handler"
    )


# --- the control-plane role --------------------------------------------------
#
# Host state moved out of this script and into the blitzecdn_controlplane
# role under src/blitzecdn/ansible/roles/, so the properties that used to be
# asserted against bash are asserted against the role's tasks. What the role
# *does* is covered by running it on a real Debian/Ubuntu host; these pin the
# invariants that a reader cannot see from one successful run.

ROLE = CORE_ANSIBLE / "roles/blitzecdn_controlplane"


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


def test_host_key_scan_is_stable_across_runs():
    """`ssh-keyscan -H` salts each run, so every converge would report a change."""
    task = _role_task("Scan the loopback host keys")
    assert "-H" not in task["ansible.builtin.command"]["argv"]
    written = _role_task("Record the loopback host keys")
    assert "sort" in written["ansible.builtin.copy"]["content"]


def test_controlplane_role_has_no_legacy_host_application_unit_cleanup():
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert "blitzecdn_controlplane_obsolete_units" not in defaults
    tasks = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    assert "obsolete host application units" not in tasks


def test_controlplane_initializes_schema_before_starting_services():
    tasks = _role_tasks()
    schema = _role_task("Initialize the application schema before starting services")
    services = _role_task("Recreate and start the control-plane services")

    assert tasks.index(schema) < tasks.index(services)
    command = schema["ansible.builtin.command"]["argv"]
    assert command[-4:] == ["--no-deps", "blitzecdn-cli", "setup", "--schema-only"]


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


def test_local_lifecycle_playbooks_do_not_load_fleet_inventory():
    """Bootstrap precedes the database, and teardown must not depend on it.

    Both convergences run through one function now, so the inventory is stated
    once — which is also what stops the two from drifting apart.
    """
    assert "-i localhost," in _function("run_playbook")
    for helper in ("converge_control_plane", "converge_uninstall"):
        assert "run_playbook" in _function(helper), (
            f"{helper} no longer runs through the shared playbook helper"
        )


def test_uninstall_succeeds_after_ansible_teardown(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    script, root = _instrument(sandbox)
    _stub_bin(sandbox, root)
    _fake_installation(root)

    result = _run_sandboxed(script, "--uninstall", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BlitzeCDN has been removed" in result.stdout


def test_edge_platform_is_pinned_to_ubuntu_26_04():
    edge = yaml.safe_load(
        (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    )
    edge_gate = edge[0]["pre_tasks"][0]["ansible.builtin.assert"]
    assert edge_gate["that"] == [
        "ansible_facts.distribution == 'Ubuntu'",
        "ansible_facts.distribution_version == '26.04'",
    ]
    assert "Ubuntu 26.04 LTS" in edge_gate["fail_msg"]


def test_no_upper_bound_on_the_python_version():
    """A ceiling is discovered on somebody's fresh server, never in CI.

    An interpreter that genuinely breaks the control plane should fail the test
    suite, not be refused at install time on a host nobody has tried yet.
    """
    script = (PROJECT_DIR / "install.sh").read_text(encoding="utf-8")
    assert "sys.version_info[:2] < (3, 12)" in script
    assert "(3, 15)" not in script, "install.sh still refuses a future Python"


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
