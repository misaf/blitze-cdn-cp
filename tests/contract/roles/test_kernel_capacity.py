"""Capacity limits and reversible host tuning, exercised through real playbooks."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from support.contracts import (
    PROJECT_DIR,
    ROLE_DIR,
    ROLES_DIR,
    role_defaults,
)
from support.edge_render import nginx_environment
from support.role_contracts import render_compose


def test_workers_and_container_share_an_overridable_file_limit():
    defaults = role_defaults(blitzecdn_edge_nofile=131072)
    rendered = (
        nginx_environment()
        .get_template("nginx.conf.j2")
        .render(blitzecdn_release_digest="test", **defaults)
    )
    assert "worker_rlimit_nofile 131072;" in rendered
    assert "worker_connections 8192;" in rendered
    limits = render_compose(blitzecdn_edge_nofile=131072)["services"]["edge"]["ulimits"]
    assert limits["nofile"] == {"soft": 131072, "hard": 131072}
    probe = yaml.safe_load((ROLE_DIR / "tasks/config-test.yml").read_text())[0]
    assert probe["community.docker.docker_container"]["ulimits"] == [
        "nofile:{{ blitzecdn_edge_runtime.nofile }}:{{ blitzecdn_edge_runtime.nofile }}"
    ]


@pytest.fixture
def kernel_host(tmp_path):
    """Run the real role against a fake kernel without changing the host."""
    binary = tmp_path / "bin"
    binary.mkdir()
    state = tmp_path / "kernel.json"
    state.write_text(
        json.dumps(
            {
                "net.core.somaxconn": "4096",
                "net.core.netdev_max_backlog": "1000",
                "net.ipv4.tcp_syncookies": "0",
                "net.ipv4.tcp_keepalive_time": "7200",
                "net.ipv4.tcp_available_congestion_control": "reno cubic",
                "net.ipv4.tcp_congestion_control": "cubic",
            }
        )
    )
    shim = f"""#!{sys.executable}
import json
import sys
from pathlib import Path
path = Path({str(state)!r})
state = json.loads(path.read_text())
args = sys.argv[1:]
if Path(sys.argv[0]).name == 'modprobe':
    if Path({str(tmp_path / "no-bbr")!r}).exists():
        sys.exit(1)
    state['net.ipv4.tcp_available_congestion_control'] = 'reno cubic bbr'
elif '-n' in args:
    key = args[-1]
    if key not in state:
        sys.exit(1)
    print(state[key])
    sys.exit(0)
elif '-w' in args:
    key, value = args[-1].split('=', 1)
    state[key] = value
else:
    sys.exit('unexpected sysctl operation: ' + repr(args))
path.write_text(json.dumps(state))
"""
    for name in ("sysctl", "modprobe"):
        path = binary / name
        path.write_text(shim)
        path.chmod(0o755)
    roles = tmp_path / "roles"
    role = roles / "blitzecdn_kernel"
    shutil.copytree(ROLES_DIR / "blitzecdn_kernel", role)
    for path in (role / "tasks").glob("*.yml"):
        # Only host paths/ownership differ: run the shipped decisions and the
        # installed sysctl module unchanged, against our fake kernel.
        text = path.read_text().replace("/usr/sbin/", str(binary) + "/")
        text = text.replace(
            "/proc/sys/net/core/netdev_max_backlog", str(tmp_path / "netdev-knob")
        )
        text = text.replace("    owner: root\n", "").replace("    group: root\n", "")
        path.write_text(text)
    dropin = tmp_path / "60-blitzecdn.conf"

    def run(settings, *, check=False):
        playbook = tmp_path / "play.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "gather_facts": False,
                        "environment": {
                            "PATH": str(binary) + os.pathsep + os.environ["PATH"]
                        },
                        "roles": ["blitzecdn_kernel"],
                        "vars": {
                            "blitzecdn_kernel_sysctl_file": str(dropin),
                            "blitzecdn_kernel_sysctl": settings,
                        },
                    }
                ]
            )
        )
        command = [
            str(PROJECT_DIR / ".venv/bin/ansible-playbook"),
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook),
        ]
        if check:
            command.append("--check")
        result = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "ANSIBLE_ROLES_PATH": str(roles),
                "ANSIBLE_COLLECTIONS_PATH": str(PROJECT_DIR / ".state/collections"),
                "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-local"),
                "ANSIBLE_NOCOLOR": "1",
            },
            cwd=tmp_path,
        )
        return result

    return run, state, dropin


def test_kernel_converges_idempotently_and_restores_removed_keys(kernel_host):
    run, state, dropin = kernel_host
    settings = {
        "net.core.somaxconn": 16384,
        "net.ipv4.tcp_keepalive_time": 90,
        "net.ipv4.tcp_syncookies": True,
    }
    first = run(settings)
    assert first.returncode == 0, first.stdout + first.stderr
    baseline = Path(str(dropin) + ".baseline.json")
    assert json.loads(baseline.read_text())["net.core.somaxconn"] == "4096"
    assert "net.ipv4.tcp_syncookies = 1" in dropin.read_text()
    second = run(settings)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "changed=0" in second.stdout
    removed = run({"net.core.somaxconn": 16384})
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert json.loads(state.read_text())["net.ipv4.tcp_keepalive_time"] == "7200"
    assert "tcp_keepalive_time" not in dropin.read_text()
    reset = run({})
    assert reset.returncode == 0, reset.stdout + reset.stderr
    assert json.loads(state.read_text())["net.core.somaxconn"] == "4096"
    assert json.loads(baseline.read_text()) == {}


def test_check_mode_does_not_write_kernel_or_baseline(kernel_host):
    run, state, dropin = kernel_host
    before = state.read_text()
    result = run({"net.core.somaxconn": 16384}, check=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == before
    assert not dropin.exists()
    assert not Path(str(dropin) + ".baseline.json").exists()


@pytest.mark.parametrize(
    "registered,available", [(True, True), (False, True), (False, False)]
)
def test_bbr_builtin_module_and_unsupported_kernel(kernel_host, registered, available):
    run, state, dropin = kernel_host
    values = json.loads(state.read_text())
    if registered:
        values["net.ipv4.tcp_available_congestion_control"] += " bbr"
        state.write_text(json.dumps(values))
    if not available or registered:
        # Built-in BBR must work even when modprobe cannot load a module.
        (dropin.parent / "no-bbr").touch()
    result = run({"net.ipv4.tcp_congestion_control": "bbr"})
    if available:
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(state.read_text())["net.ipv4.tcp_congestion_control"] == "bbr"
    else:
        assert result.returncode != 0
        assert (
            json.loads(state.read_text())["net.ipv4.tcp_congestion_control"] == "cubic"
        )
        assert not dropin.exists()


@pytest.mark.parametrize("exposed", [False, True])
def test_input_backlog_is_applied_only_when_the_namespace_exposes_it(
    kernel_host, exposed
):
    run, state, dropin = kernel_host
    if exposed:
        (dropin.parent / "netdev-knob").touch()
    result = run({"net.core.somaxconn": 16384, "net.core.netdev_max_backlog": 16384})
    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads(state.read_text())
    assert values["net.core.somaxconn"] == "16384"
    assert values["net.core.netdev_max_backlog"] == ("16384" if exposed else "1000")
    assert ("net.core.netdev_max_backlog" in dropin.read_text()) == exposed
    baseline = json.loads(Path(str(dropin) + ".baseline.json").read_text())
    assert ("net.core.netdev_max_backlog" in baseline) == exposed
