import json
import signal
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from blitzecdn.domain.runs import RunStatus
from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure import ansible


def _callback_result(environment, hosts):
    """Write what the `blitzecdn_result` callback would have written.

    The runner reads its result from the path it puts in BLITZE_RESULT_PATH, so
    a double for the child process has to produce a document there — that file
    is the contract, and nothing about the terminal output matters any more.
    """
    Path(environment["BLITZE_RESULT_PATH"]).write_text(
        json.dumps({"playbook": "edge.yml", "hosts": hosts}), encoding="utf-8"
    )


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


def test_runner_builds_a_check_command_and_keeps_the_raw_log(settings, monkeypatch):
    """Output goes to a log file; the result comes from the callback document."""
    runner = ansible.AnsibleRunner(settings)
    captured = []

    streams = {}

    def fake_popen(command, **kwargs):
        captured.extend(command)
        streams["stderr"] = kwargs["stderr"]
        kwargs["stdout"].write(b"TASK [something] ***\nchanged: [edge-a]\n")
        _callback_result(kwargs["env"], [{"host": "edge-a", "ok": 4, "changed": 1}])
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    run = runner.run(check=True)

    assert "--check" in captured and "--diff" in captured
    assert [host.host for host in run.hosts] == ["edge-a"]
    assert run.hosts[0].changed == 1
    # One file, not two: stderr is folded into the same stream so the log reads
    # in the order things happened rather than needing to be interleaved later.
    assert streams["stderr"] is subprocess.STDOUT
    assert Path(run.log_path).read_text(encoding="utf-8").startswith("TASK [")


def test_the_result_document_is_removed_once_it_has_been_read(settings, monkeypatch):
    """It is working state: what survives is the parsed result and the log."""
    seen: list[Path] = []

    def fake_popen(command, **kwargs):
        seen.append(Path(kwargs["env"]["BLITZE_RESULT_PATH"]))
        _callback_result(kwargs["env"], [{"host": "edge-a"}])
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    ansible.AnsibleRunner(settings).run(check=True)

    assert seen and not seen[0].exists()


def test_each_run_gets_a_result_path_of_its_own(settings, monkeypatch):
    """Purge, stats and decommission all skip the lock, so runs overlap."""
    paths: list[str] = []

    def fake_popen(command, **kwargs):
        paths.append(kwargs["env"]["BLITZE_RESULT_PATH"])
        _callback_result(kwargs["env"], [{"host": "edge-a"}])
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    runner = ansible.AnsibleRunner(settings)
    runner.run(check=True)
    runner.run(check=True)

    assert len(set(paths)) == 2


def test_a_run_that_reports_nothing_says_where_to_look(settings, monkeypatch):
    """Ansible died before reporting: there is no host data, only the log."""

    def fake_popen(command, **kwargs):
        kwargs["stdout"].write(b"ERROR! the inventory could not be read\n")
        return FakePopen(command, return_code=4, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    run = ansible.AnsibleRunner(settings).run(check=False)

    assert run.status is RunStatus.FAILED
    assert run.reported is False
    assert run.hosts == ()
    assert run.log_path is not None and run.log_path in (run.error or "")


def test_an_unparseable_result_is_treated_as_no_result(settings, monkeypatch):
    """A half-written document must not become half-believed host data."""

    def fake_popen(command, **kwargs):
        Path(kwargs["env"]["BLITZE_RESULT_PATH"]).write_text(
            '{"hosts": [', encoding="utf-8"
        )
        return FakePopen(command, return_code=2, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    run = ansible.AnsibleRunner(settings).run(check=False)

    assert run.reported is False
    assert run.status is RunStatus.FAILED


def test_run_logs_are_pruned_to_the_retention_limit(settings, monkeypatch):
    """One log per invocation, and the drift timer alone fires on a schedule."""
    bounded = settings.model_copy(update={"run_log_retention": 10})

    def fake_popen(command, **kwargs):
        _callback_result(kwargs["env"], [{"host": "edge-a"}])
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    runner = ansible.AnsibleRunner(bounded)
    for _ in range(14):
        runner.run(check=True)

    assert len(list(bounded.log_dir.glob("*.log"))) == 10


def test_each_run_uses_an_isolated_ssh_control_path(settings, monkeypatch):
    control_paths: list[str] = []

    def fake_popen(command, **kwargs):
        control_paths.append(kwargs["env"]["ANSIBLE_SSH_CONTROL_PATH_DIR"])
        assert Path(control_paths[-1]).is_dir()
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    runner = ansible.AnsibleRunner(settings)
    runner.run(check=True)
    runner.run(check=True)

    assert len(set(control_paths)) == 2
    assert all(not Path(path).exists() for path in control_paths)


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

    run = ansible.AnsibleRunner(settings).run(check=False)

    assert run.status is RunStatus.TIMED_OUT
    assert run.return_code == 124
    # The group, not just the direct child: start_new_session makes the playbook
    # its own group leader, so its per-host workers share this pid as their pgid.
    assert killed == [(process.pid, signal.SIGTERM)]


def test_runner_escalates_to_sigkill_when_ansible_ignores_sigterm(
    settings, monkeypatch
):
    process = FakePopen(["ansible-playbook"], hangs=True)
    killed = _capture_killpg(monkeypatch, process, dies_on_sigterm=False)

    timed_out = ansible.AnsibleRunner(settings).run(check=False)
    assert timed_out.status is RunStatus.TIMED_OUT
    assert killed == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]


def test_runner_kills_the_process_group_when_the_operator_interrupts(
    settings, monkeypatch
):
    process = FakePopen(["ansible-playbook"])

    def interrupted_wait(timeout=None):
        if timeout == settings.deployment_timeout_seconds:
            raise KeyboardInterrupt
        return 0

    process.wait = interrupted_wait
    killed = _capture_killpg(monkeypatch, process, dies_on_sigterm=True)

    with pytest.raises(KeyboardInterrupt):
        ansible.AnsibleRunner(settings).run(check=False)

    assert killed == [(process.pid, signal.SIGTERM)]


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
        ansible.AnsibleRunner(broken).validate(broken.generated_vars_path)


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
    assert result.status is RunStatus.SUCCEEDED
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


# A document in the shape `blitzecdn_result` writes. This is the seam the
# control plane depends on now, so it is pinned here rather than assumed.
_RESULT = {
    "playbook": "edge.yml",
    "hosts": [
        {
            "host": "edge-a",
            "ok": 14,
            "changed": 2,
            "skipped": 3,
            "changes": [
                {"task": "Render managed sites", "outcome": "changed"},
                {"task": "Enable desired sites", "outcome": "changed"},
            ],
        },
        {"host": "edge-b", "ok": 14, "changed": 0, "skipped": 3},
        {
            "host": "edge-c",
            "unreachable": 1,
            "failures": [
                {
                    "task": "Gathering Facts",
                    "outcome": "unreachable",
                    "message": "ssh: connect to host edge-c port 22: No route to host",
                }
            ],
        },
    ],
}


def test_the_callback_document_becomes_per_host_results(settings, monkeypatch):
    def fake_popen(command, **kwargs):
        Path(kwargs["env"]["BLITZE_RESULT_PATH"]).write_text(
            json.dumps(_RESULT), encoding="utf-8"
        )
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    run = ansible.AnsibleRunner(settings).run(check=True)

    hosts = {host.host: host for host in run.hosts}
    assert set(hosts) == {"edge-a", "edge-b", "edge-c"}
    assert hosts["edge-a"].changed == 2
    assert hosts["edge-a"].in_sync is False
    assert hosts["edge-b"].in_sync is True
    assert hosts["edge-c"].reached is False
    assert hosts["edge-c"].in_sync is False, "an unreachable host is not 'in sync'"


def test_a_change_is_named_not_merely_counted(settings, monkeypatch):
    """The reason for a callback rather than a recap.

    A recap could say "edge-a would change 2 tasks" and no more. Naming them is
    what makes a drift report answer the question an operator actually has.
    """

    def fake_popen(command, **kwargs):
        Path(kwargs["env"]["BLITZE_RESULT_PATH"]).write_text(
            json.dumps(_RESULT), encoding="utf-8"
        )
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    run = ansible.AnsibleRunner(settings).run(check=True)

    assert [change.task for change in run.host("edge-a").changes] == [
        "Render managed sites",
        "Enable desired sites",
    ]
    assert "No route to host" in run.summary()


def test_a_role_payload_arrives_on_the_host_that_published_it(settings, monkeypatch):
    """`blitzecdn_report` is how a role returns data rather than an outcome."""

    def fake_popen(command, **kwargs):
        Path(kwargs["env"]["BLITZE_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "hosts": [
                        {"host": "edge-a", "report": {"nginx_reachable": True}},
                        {"host": "edge-b"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    run = ansible.AnsibleRunner(settings).run(check=True)

    assert run.host("edge-a").report == {"nginx_reachable": True}
    assert run.host("edge-b").report is None


def test_maxmind_credentials_reach_ansible_through_the_environment(
    settings, monkeypatch
):
    """`.env` has to survive the hop into the Ansible subprocess.

    Settings parses `.env` into its own mapping, not into os.environ, and the
    runner builds the child environment from os.environ — so a credential set
    in `.env` reaches Ansible only because the runner forwards it explicitly.
    group_vars reads it with `lookup('env', ...)`, which yields an empty string
    if this regresses, leaving country filtering silently unconfigured instead
    of failing.
    """
    configured = settings.model_copy(
        update={
            "maxmind_account_id": "123456",
            "maxmind_license_key": SecretStr("SENTINELKEY"),
        }
    )
    captured: dict[str, str] = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs["env"])
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    ansible.AnsibleRunner(configured).run(check=True)

    assert captured["BLITZE_MAXMIND_ACCOUNT_ID"] == "123456"
    assert captured["BLITZE_MAXMIND_LICENSE_KEY"] == "SENTINELKEY"


def test_maxmind_credentials_never_become_command_arguments(settings, monkeypatch):
    """A secret in argv is readable by every account on the controller.

    Keeping it out of the process table is the whole reason it travels in the
    environment rather than as `--extra-vars`.
    """
    configured = settings.model_copy(
        update={"maxmind_license_key": SecretStr("SENTINELKEY")}
    )
    captured: list[str] = []

    def fake_popen(command, **kwargs):
        captured.extend(command)
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    ansible.AnsibleRunner(configured).run(check=True)

    assert captured
    assert not any("SENTINELKEY" in argument for argument in captured)


def test_the_credential_environment_is_set_even_when_unconfigured(
    settings, monkeypatch
):
    """The runner forwards resolved settings, not whatever is in the ambient env.

    `Settings` resolves the credential once, letting a real environment
    variable win over `.env`. After that the runner must overwrite the child's
    value unconditionally: inheriting os.environ and only setting the key when
    non-empty would let a stale export from the deploying shell reach Ansible,
    so the same desired state would converge differently depending on who ran
    it.
    """
    monkeypatch.setenv("BLITZE_MAXMIND_LICENSE_KEY", "FROM-THE-SHELL")
    captured: dict[str, str] = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs["env"])
        return FakePopen(command, **kwargs)

    monkeypatch.setattr(ansible.subprocess, "Popen", fake_popen)
    ansible.AnsibleRunner(settings).run(check=True)

    assert captured["BLITZE_MAXMIND_LICENSE_KEY"] == ""
