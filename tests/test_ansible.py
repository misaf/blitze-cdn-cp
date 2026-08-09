import signal
import subprocess

import pytest

from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure import ansible


class FakePopen:
    """Stand-in for the ansible-playbook child process.

    ``hangs`` makes every bounded wait time out until the process is reaped,
    which models a playbook that outlives its deadline.
    """

    def __init__(self, command, *, return_code=0, hangs=False, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242
        self._return_code = return_code
        self._running = hangs

    def reap(self) -> None:
        self._running = False

    def wait(self, timeout=None):
        if self._running and timeout is not None:
            raise subprocess.TimeoutExpired("ansible", timeout)
        return self._return_code


def test_deployment_lock_serializes_processes(settings):
    first = ansible.DeploymentLock(settings.deployment_lock_path)
    with (
        first,
        pytest.raises(DeploymentBusyError),
        ansible.DeploymentLock(settings.deployment_lock_path),
    ):
        pass
    with ansible.DeploymentLock(settings.deployment_lock_path):
        pass


def test_runner_builds_bounded_check_command(settings, monkeypatch):
    runner = ansible.AnsibleRunner(settings)
    captured = []

    def fake_popen(command, **kwargs):
        captured.extend(command)
        kwargs["stdout"].write(b"x" * (settings.output_limit_bytes + 10))
        kwargs["stderr"].write(b"warning")
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    result = runner.run(check=True)
    assert "--check" in captured and "--diff" in captured
    assert result.stdout.endswith("[output truncated]")
    assert result.stderr == "warning"


def _capture_killpg(monkeypatch, process, *, dies_on_sigterm):
    killed: list[tuple[int, int]] = []

    def killpg(pid, signal_number):
        killed.append((pid, signal_number))
        if dies_on_sigterm or signal_number == signal.SIGKILL:
            process.reap()

    monkeypatch.setattr(ansible.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr("blitzecdn.infrastructure.process.os.killpg", killpg)
    return killed


def test_runner_kills_the_whole_process_group_on_timeout(settings, monkeypatch):
    """A timed-out playbook must not leave workers converging edges behind us."""
    process = FakePopen(["ansible-playbook"], hangs=True)
    killed = _capture_killpg(monkeypatch, process, dies_on_sigterm=True)

    result = ansible.AnsibleRunner(settings).run(check=False)

    assert result.timed_out is True
    assert result.return_code == 124
    # The group, not just the direct child: start_new_session makes the playbook
    # its own group leader, so its per-host workers share this pid as their pgid.
    assert killed == [(process.pid, signal.SIGTERM)]


def test_runner_escalates_to_sigkill_when_ansible_ignores_sigterm(
    settings, monkeypatch
):
    process = FakePopen(["ansible-playbook"], hangs=True)
    killed = _capture_killpg(monkeypatch, process, dies_on_sigterm=False)

    assert ansible.AnsibleRunner(settings).run(check=False).timed_out is True
    assert killed == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]


def test_runner_translates_os_errors(settings, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(ansible.subprocess, "Popen", fail)
    with pytest.raises(ExecutionError, match="unable to execute"):
        ansible.AnsibleRunner(settings).run(check=False)


def test_runner_validates_required_paths(settings):
    broken = settings.model_copy(
        update={"inventory_path": settings.project_dir / "missing"}
    )
    with pytest.raises(ConfigurationError, match="inventory"):
        ansible.AnsibleRunner(broken).validate()


def test_runner_builds_acme_challenge_command(settings, monkeypatch):
    settings.acme_challenge_playbook_path.write_text(
        "- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8"
    )
    captured = []

    def fake_popen(command, **kwargs):
        captured.extend(command)
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    result = ansible.AnsibleRunner(settings).run_acme_challenge(
        action="present",
        domain="cdn.example.com",
        token="safe_token",  # noqa: S106 -- public ACME challenge token
        validation="safe.validation",
    )
    assert result.return_code == 0
    assert str(settings.acme_challenge_playbook_path) in captured
    assert "safe_token" not in captured


def _with_edges(settings, *names: str) -> None:
    hosts = "".join(
        f"        {name}:\n          ansible_host: 198.51.100.{index}\n"
        for index, name in enumerate(names, start=1)
    )
    settings.inventory_path.write_text(
        f"all:\n  children:\n    blitzecdn_edges:\n      hosts:\n{hosts}",
        encoding="utf-8",
    )


def test_a_run_without_a_limit_targets_the_whole_edge_group(settings):
    runner = ansible.AnsibleRunner(settings)
    assert runner._limit(None) == "blitzecdn_edges"
    assert runner._limit("  ") == "blitzecdn_edges"


def test_a_limit_resolves_to_the_matching_edges(settings):
    _with_edges(settings, "edge-a", "edge-b", "other")
    runner = ansible.AnsibleRunner(settings)
    assert runner._limit("edge-a") == "edge-a"
    assert runner._limit("edge-a,other") == "edge-a,other"
    assert runner._limit("edge-*") == "edge-a,edge-b"


def test_a_limit_cannot_reach_a_host_outside_the_edge_group(settings):
    """The whole point of resolving against the inventory rather than passing
    a pattern through: an unknown name is refused, not silently targeted."""
    _with_edges(settings, "edge-a")
    runner = ansible.AnsibleRunner(settings)
    with pytest.raises(ConfigurationError, match="matches none of the configured"):
        runner._limit("database-1")


@pytest.mark.parametrize(
    "pattern",
    [
        "edge-a:database-1",  # union, would add a host outside the group
        "edge-a:!edge-b",  # exclusion
        "all",  # not a name we would resolve, but spelled out for intent
        "@/etc/passwd",  # read hosts from a file
        "edge a",  # whitespace
        "edge-a&database-1",
    ],
)
def test_limit_patterns_that_could_widen_a_deploy_are_refused(settings, pattern):
    _with_edges(settings, "edge-a", "edge-b")
    runner = ansible.AnsibleRunner(settings)
    with pytest.raises((ConfigurationError, ValueError)):
        runner._limit(pattern)


def test_the_limit_reaches_the_ansible_command_line(settings, monkeypatch):
    _with_edges(settings, "edge-a", "edge-b")
    (settings.ansible_dir / "playbooks/edge.yml").write_text("---\n", encoding="utf-8")
    settings.generated_vars_path.parent.mkdir(parents=True, exist_ok=True)
    settings.generated_vars_path.write_text("---\n", encoding="utf-8")
    captured: list[list[str]] = []

    def fake_popen(command, **kwargs):
        captured.append(command)
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        ansible.shutil, "which", lambda _name: "/usr/bin/ansible-playbook"
    )
    ansible.AnsibleRunner(settings).run(check=True, host_limit="edge-a")

    command = captured[0]
    assert command[command.index("--limit") + 1] == "edge-a"
    assert command.count("--limit") == 1


# Verbatim Ansible output, with the host-name padding trimmed to fit. The
# trailing skipped/rescued/ignored fields are kept deliberately: the parser has
# to tolerate counters it does not care about.
_RECAP = """
TASK [blitzecdn.edge.blitzecdn_nginx : Render managed sites] ****
changed: [edge-a]
ok: [edge-b]

PLAY RECAP *********************************************************************
edge-a : ok=14  changed=2  unreachable=0  failed=0  skipped=3  rescued=0  ignored=0
edge-b : ok=14  changed=0  unreachable=0  failed=0  skipped=3  rescued=0  ignored=0
edge-c : ok=0   changed=0  unreachable=1  failed=0  skipped=0  rescued=0  ignored=0
"""


def test_the_play_recap_becomes_per_host_drift():
    hosts = {host.host: host for host in ansible.parse_play_recap(_RECAP)}
    assert set(hosts) == {"edge-a", "edge-b", "edge-c"}
    assert hosts["edge-a"].changed == 2
    assert hosts["edge-a"].in_sync is False
    assert hosts["edge-b"].in_sync is True
    assert hosts["edge-c"].unreachable == 1
    assert hosts["edge-c"].in_sync is False, "an unreachable host is not 'in sync'"


def test_only_the_last_recap_is_read():
    """A run with several plays prints one recap each; the last is cumulative."""
    doubled = (
        _RECAP + "\nPLAY RECAP ****\nedge-a : ok=1 changed=0 unreachable=0 failed=0\n"
    )
    hosts = ansible.parse_play_recap(doubled)
    assert [host.host for host in hosts] == ["edge-a"]
    assert hosts[0].changed == 0


def test_output_without_a_recap_yields_no_hosts():
    assert ansible.parse_play_recap("ERROR! the playbook could not be parsed") == ()
    assert ansible.parse_play_recap("") == ()
