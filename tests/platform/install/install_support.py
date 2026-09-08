"""Helpers shared by the install.sh suite.

The script refuses to run privileged subcommands as a normal user, so what the
tests can drive is everything that decides *whether* to provision — dispatch,
argument parsing, and the validators the script shells out to Python for. The
first group of helpers extracts and runs those directly from the source.

The second group builds the sandbox the lifecycle paths need: the script copied
with every path it touches redirected under a temp directory, the root check
neutralised, and the privileged commands (systemctl, userdel, nginx, git)
stubbed on ``PATH``. That is what lets a test verify what is actually removed,
and that a reinstall takes the same path as a brand-new server, without
touching the host.

Helpers used by one module only stay beside its tests.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
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
        # The standalone platform gate reads this, and `--fresh` runs the gate
        # before it tears anything down. Redirected like every other absolute
        # path so the sandbox can say what host it is pretending to be.
        ("/etc/os-release", root / "etc/os-release"),
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
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc/os-release").write_text(
        'ID=ubuntu\nVERSION_ID="26.04"\n', encoding="utf-8"
    )
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


# --- the update sandbox ------------------------------------------------------


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
        #
        # Filtered by the glob the caller passed, because `latest_release_tag`
        # is asked about one major at a time and an upgrade asks about two —
        # its own line and the next. A stub that answered both questions with
        # one list could not tell them apart, and the test that matters most
        # here is precisely that a v3 host is offered v4 and not v3.
        '    for pattern in "$@"; do case "${pattern}" in v*.\\*) break ;; esac; done\n'
        '    printf "%b" "${UPDATE_GIT_TAGS-v3.1.0\\nv3.0.0\\n}" |\n'
        "      while IFS= read -r line; do\n"
        "        [[ -z ${line} ]] && continue\n"
        # shellcheck-free glob match: the pattern is `v3.*` and the tag `v3.1.0`.
        '        case "${line}" in ${pattern}) printf "%s\\n" "${line}" ;; esac\n'
        "      done\n"
        "    exit 0 ;;\n"
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
        # Separately, and only when asked: the order log is compared by exact
        # line elsewhere, and what `update` and `upgrade` differ in is not
        # whether a backup happened but which one.
        'printf "%s\\n" "$*" >> "${UPDATE_BACKUP_ARGS_LOG:-/dev/null}"\n'
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


# --- the control-plane role --------------------------------------------------

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
