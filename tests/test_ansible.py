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
