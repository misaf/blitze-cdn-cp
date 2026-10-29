"""Helpers shared by the install.sh suite.

The script refuses privileged subcommands as a normal user, so the first group
of helpers drives what decides whether to provision: dispatch, argument
parsing, and the validators the script runs through Python.

The second group builds a sandbox for the lifecycle paths: the script copied
with every path redirected under a temp directory, the root check neutralised,
and privileged commands (systemctl, userdel, nginx, docker) stubbed on
``PATH``.
"""

import os
import re
import shlex
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


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def query(expression: str) -> str:
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


def script_section(name: str) -> str:
    """Return one subcommand's body.

    Scoped so assertions about what one subcommand must never do do not trip on
    another's legitimate code.
    """
    remainder = script_text()[script_text().index(f"cmd_{name}() {{") :]
    banner = re.search(r"\n# -{10,}", remainder)
    return remainder if banner is None else remainder[: banner.start()]


def script_function(name: str) -> str:
    """Return one named function's body, braces stripped.

    Bounded as the script bounds it: by a closing brace at column zero.
    """
    script = script_text()
    start = script.index(f"{name}() {{") + len(f"{name}() {{")
    closing = script.index("\n}\n", start)
    return script[start:closing]


def host_helper(helper: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one of the stdlib host helpers the way the script does: ``python3 -I``."""
    return subprocess.run(  # noqa: S603 - interpreter is sys.executable
        [sys.executable, "-I", str(SOURCE / "host" / f"{helper}.py"), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


# --- sandbox for the lifecycle paths ------------------------------------------
#
# `--uninstall` and `update` run for real inside a redirected copy of the
# script with every privileged command stubbed.


def instrument(sandbox: Path) -> tuple[Path, Path]:
    """Copy install.sh with every path redirected under ``sandbox/root``.

    The root check becomes a tautology so a normal user can exercise the
    destructive paths against stubs. Returns the script and the redirect root.
    """
    sandbox.mkdir(parents=True, exist_ok=True)
    root = sandbox / "root"
    script = sandbox / "install.sh"
    text = SCRIPT.read_text(encoding="utf-8")
    for real, redirected in (
        # Read by the platform gate and by the Docker bootstrap; redirected so
        # the sandbox can choose the host it pretends to be.
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
        (
            "/usr/local/sbin/blitzecdn-install",
            root / "usr/local/sbin/blitzecdn-install",
        ),
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


#: The release a fake installation runs, and the image its Compose project
#: records.
FAKE_RELEASE = "3.0.0"
FAKE_IMAGE = f"ghcr.io/misaf/blitzecdn-control-plane:{FAKE_RELEASE}"


def stub_bin(sandbox: Path, root: Path) -> None:
    """Provide stand-ins for every privileged command the destructive paths run."""
    bindir = sandbox / "bin"
    bindir.mkdir(exist_ok=True)
    # The engine. DOCKER_IMAGE_MISSING makes every image lookup fail; a play run
    # in the image is the fake installation's stand-in for the uninstall play,
    # and every `docker` call is recorded for the tests that order them.
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "docker $*" >> "${DOCKER_LOG:-/dev/null}"\n'
        'case "$*" in\n'
        '  "image inspect"*)\n'
        '    [[ -z "${DOCKER_IMAGE_MISSING:-}" ]] || exit 1\n'
        "    echo sha256:0123; exit 0 ;;\n"
        "  run*ansible-playbook*uninstall.yml*)\n"
        f'    exec "{sandbox / "uninstall-play"}" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
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
    for path in bindir.iterdir():
        path.chmod(0o700)


def workspace_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def fake_installation(root: Path) -> list[Path]:
    """Create every artifact a standalone install owns on the host."""
    owned = [
        root / "etc/blitzecdn/firewall-rules",
        root / "etc/blitzecdn/control-plane.compose.yml",
        root / "etc/systemd/system/blitzecdn-geoipupdate.service",
        root / "etc/systemd/system/blitzecdn-geoipupdate.timer",
        root / "usr/local/bin/blitzecdn",
        root / "usr/local/sbin/blitzecdn-install",
        root / "etc/sudoers.d/blitzecdn-deploy",
        root / "etc/nginx/blitzecdn-managed-sites",
        root / "etc/nginx/conf.d/blitzecdn-feature-cache-http.conf",
        root / "etc/nginx/conf.d/blitzecdn-feature-geoip-http.conf",
        root / "etc/nginx/conf.d/blitzecdn-status.conf",
        root / "etc/nginx/sites-available/example-cdn.conf",
        root / "etc/nginx/sites-enabled/example-cdn.conf",
        root / "var/cache/nginx/blitzecdn/cache-data",
        root / "etc/systemd/resolved.conf.d/blitzecdn.conf",
        root / "var/log/nginx/blitzecdn-access.log",
        root / "etc/fail2ban/jail.d/blitzecdn-sshd.local",
        root / "etc/ssh/sshd_config.d/50-blitzecdn.conf",
        root / "etc/sysctl.d/60-blitzecdn.conf",
        root / "var/lib/blitzecdn/control-plane.db",
        root / "var/lib/blitzecdn/certificates/example-cdn/fullchain-deadbeef.pem",
        root / "var/lib/blitzecdn/.ssh/known_hosts",
        root / "var/lib/blitzecdn/.ssh/config",
        root / "var/lib/blitzecdn/acme/.well-known/acme-challenge/token",
        root / "home/deploy/.ssh/authorized_keys",
    ]
    for path in owned:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Managed by BlitzeCDN. Local edits are overwritten.\nx\n",
            encoding="utf-8",
        )
    # Where the installer reads back which image the services run, written as
    # the role's template writes it.
    (root / "etc/blitzecdn/control-plane.compose.yml").write_text(
        "# Managed by BlitzeCDN.\n"
        'name: "blitzecdn-control-plane"\n'
        "x-blitzecdn-runtime: &blitzecdn-runtime\n"
        f'  image: "{FAKE_IMAGE}"\n',
        encoding="utf-8",
    )
    # The installer owns only invoking the play and removing the image. This
    # stand-in for the play the docker stub runs in the image models a
    # successful uninstall by removing every path in the fixture.
    ansible = root.parent / "uninstall-play"
    ansible.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + "rm -rf -- "
        + " ".join(shlex.quote(str(path)) for path in owned)
        + "\n",
        encoding="utf-8",
    )
    ansible.chmod(0o700)
    return owned


def fake_unrelated(root: Path) -> list[Path]:
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


def run_sandboxed(
    script: Path,
    *arguments: str,
    input: str | None = None,  # noqa: A002 - mirrors subprocess.run
    env_extra: dict[str, str] | None = None,
    bin_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
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


def _stub_update_services(sandbox: Path, root: Path) -> None:
    """Docker answering the questions a release move asks, and a recording CLI.

    The image helpers answer from the environment: ``UPDATE_TARGET`` is what
    `image_releases` selects, and ``UPDATE_SELECT_ERROR`` makes it refuse. The
    installer never stops a service (the role does), so anything that reaches
    the engine beyond those questions shows up in the order log. `dpkg` fails,
    which stops a run at the Docker bootstrap, past its point of no return.
    """
    (sandbox / "bin" / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "image inspect"*)\n'
        '    [[ -z "${DOCKER_IMAGE_MISSING:-}" ]] || exit 1\n'
        "    exit 0 ;;\n"
        "  run*blitzecdn.host.install_inputs*)\n"
        "    exit 0 ;;\n"
        "  run*blitzecdn.host.image_releases*)\n"
        '    printf "%s\\n" "$*" >> "${UPDATE_SELECT_LOG:-/dev/null}"\n'
        '    if [[ -n "${UPDATE_SELECT_ERROR:-}" ]]; then\n'
        '      echo "error: ${UPDATE_SELECT_ERROR}; nothing was changed" >&2; exit 1\n'
        "    fi\n"
        '    echo "${UPDATE_TARGET:-3.1.0}"; exit 0 ;;\n'
        "esac\n"
        'printf "%s\\n" "docker $*" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (sandbox / "bin" / "dpkg").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "docker bootstrap" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    for stub in ("docker", "dpkg"):
        (sandbox / "bin" / stub).chmod(0o700)
    wrapper = root / "usr/local/bin/blitzecdn"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "backup" >> "${UPDATE_ORDER_LOG:-/dev/null}"\n'
        # Only when asked: a plain and a major update take different backups.
        'printf "%s\\n" "$*" >> "${UPDATE_BACKUP_ARGS_LOG:-/dev/null}"\n'
        'exit "${UPDATE_BACKUP_STATUS:-0}"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)


def update_sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An installed host on `FAKE_RELEASE`, with its installer instrumented.

    Returns the script, the stub directory, and the root.
    """
    sandbox = tmp_path / "sandbox"
    script, root = instrument(sandbox)
    stub_bin(sandbox, root)
    fake_installation(root)
    _stub_update_services(sandbox, root)
    return script, sandbox / "bin", root


# --- the control-plane role --------------------------------------------------

ROLE = CORE_ANSIBLE / "roles/blitzecdn_controlplane"


def role_tasks() -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8")),
    )


def role_task(name: str) -> dict[str, Any]:
    matches = [task for task in role_tasks() if task.get("name") == name]
    assert len(matches) == 1, f"expected exactly one task named {name!r}"
    return matches[0]


def evaluate_os_gate(distribution: str, major_version: str) -> bool:
    """Evaluate the role's supported-OS expression as Ansible would."""
    task = role_task("Validate supported operating system")
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
