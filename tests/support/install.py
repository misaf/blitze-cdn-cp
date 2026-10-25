"""Helpers shared by the install.sh suite.

The script refuses privileged subcommands as a normal user, so the first group
of helpers drives what decides whether to provision: dispatch, argument
parsing, and the validators the script runs through Python.

The second group builds a sandbox for the lifecycle paths: the script copied
with every path redirected under a temp directory, the root check neutralised,
and privileged commands (systemctl, userdel, nginx, git) stubbed on ``PATH``.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import jinja2
import yaml

from support.paths import CORE_ANSIBLE, REPO_ROOT, SOURCE

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

    Dispatch is guarded on ``BASH_SOURCE[0] == $0``, so sourcing defines
    everything and provisions nothing, and a helper can be tested by calling it.
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

    Scoped so assertions about what one subcommand must never do do not trip on
    another's legitimate code.
    """
    remainder = _script()[_script().index(f"cmd_{name}() {{") :]
    banner = re.search(r"\n# -{10,}", remainder)
    return remainder if banner is None else remainder[: banner.start()]


def _function(name: str) -> str:
    """Return one named function's body, braces stripped.

    Bounded as the script bounds it: by a closing brace at column zero.
    """
    script = _script()
    start = script.index(f"{name}() {{") + len(f"{name}() {{")
    closing = script.index("\n}\n", start)
    return script[start:closing]


def _host_helper(helper: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one of the stdlib host helpers the way the script does: ``python3 -I``."""
    return subprocess.run(  # noqa: S603 - interpreter is sys.executable
        [sys.executable, "-I", str(SOURCE / "host" / f"{helper}.py"), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


# --- sandbox for the destructive paths ---------------------------------------
#
# `--uninstall` and `--fresh` run for real inside a redirected copy of the
# script with every privileged command stubbed.


def _instrument(sandbox: Path) -> tuple[Path, Path]:
    """Copy install.sh with every path redirected under ``sandbox/root``.

    The root check becomes a tautology so a normal user can exercise the
    destructive paths against stubs. Returns the script and the redirect root.
    """
    sandbox.mkdir(parents=True, exist_ok=True)
    root = sandbox / "root"
    script = sandbox / "install.sh"
    text = SCRIPT.read_text(encoding="utf-8")
    for real, redirected in (
        # Read by the standalone platform gate, which `--fresh` runs before tearing
        # down; redirected so the sandbox can choose the host it pretends to be.
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
        '  *"show HEAD:pyproject.toml"*)\n'
        f"    echo 'version = \"{_workspace_version()}\"'; exit 0 ;;\n"
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


def _workspace_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


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
        root / "opt/blitzecdn/src/blitzecdn/assets/ansible/playbooks/uninstall.yml",
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
    # The real host helpers, where the script runs them from, answering
    # through the git stub.
    helpers = root / "opt/blitzecdn/src/blitzecdn/host"
    helpers.mkdir(parents=True, exist_ok=True)
    for helper in ("install_release.py", "install_inputs.py"):
        shutil.copyfile(SOURCE / "host" / helper, helpers / helper)
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
    """Answer the questions a release move asks, driven by environment variables.

    The arm order is load-bearing, and every mistake fails as a silently wrong
    answer:

    * `*fetch*` stays above the tag arm, and the tag arm matches ``tag --list``,
      because the real call is ``fetch --tags --prune origin``.
    * ``--exact-match`` precedes ``--abbrev=0``: both contain
      ``describe --tags``.
    * The ``HEAD..`` form of ``rev-list --count`` is matched before the general
      one.
    * Patterns start with ``*`` because ``$*`` carries `repo_git`'s
      ``-c safe.directory=... -C <path>`` prefix.
    * The unset-only default (``${VAR-x}``) lets a test pass ``""`` to mean "this
      question has no answer".
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
        # %b, not %s: the default carries escaped newlines that must become real ones.
        #
        # Filtered by the caller's glob, because `latest_release_tag` is asked about one
        # major at a time and an upgrade asks about two.
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
    """Docker, which answers nothing, and the CLI wrapper, which records.

    The installer never stops a service (the role does), so a `stop` reaching this
    stub shows up in the order log.
    """
    (sandbox / "bin" / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "docker $*" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (sandbox / "bin" / "docker").chmod(0o700)
    wrapper = root / "usr/local/bin/blitzecdn"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "backup" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        # Only when asked: `update` and `upgrade` differ in which backup they take.
        'printf "%s\\n" "$*" >> "${UPDATE_BACKUP_ARGS_LOG:-/dev/null}"\n'
        'exit "${UPDATE_BACKUP_STATUS:-0}"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)


def _update_sandbox(
    tmp_path: Path, *, with_git: bool = True
) -> tuple[Path, Path, Path]:
    """Build an installation whose install.sh *is* the instrumented script.

    `update` only runs from the installation directory it updates. Returns the
    script, the stub directory, and the root.
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


def _role_tasks() -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8")),
    )


def _role_task(name: str) -> dict[str, Any]:
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
