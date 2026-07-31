import subprocess

import pytest

from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure import ansible


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

    def fake_run(command, **kwargs):
        captured.extend(command)
        kwargs["stdout"].write(b"x" * (settings.output_limit_bytes + 10))
        kwargs["stderr"].write(b"warning")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ansible.subprocess, "run", fake_run)
    result = runner.run(check=True)
    assert "--check" in captured and "--diff" in captured
    assert result.stdout.endswith("[output truncated]")
    assert result.stderr == "warning"


def test_runner_translates_timeout_and_os_errors(settings, monkeypatch):
    runner = ansible.AnsibleRunner(settings)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ansible", 1)

    monkeypatch.setattr(ansible.subprocess, "run", timeout)
    assert runner.run(check=False).timed_out is True

    def fail(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(ansible.subprocess, "run", fail)
    with pytest.raises(ExecutionError, match="unable to execute"):
        runner.run(check=False)


def test_runner_validates_required_paths(settings):
    broken = settings.model_copy(
        update={"inventory_path": settings.project_dir / "missing"}
    )
    with pytest.raises(ConfigurationError, match="inventory"):
        ansible.AnsibleRunner(broken).validate()
