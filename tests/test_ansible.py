import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FakeEdgeStore, edge
from pydantic import SecretStr

from blitzecdn.domain.runs import RunStatus
from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure import ansible

#: The package under test, for the subprocess in the lock test below.
PROJECT_SRC = Path(__file__).resolve().parent.parent / "src"


def _callback_result(environment, hosts):
    """Write what the `blitzecdn_result` callback would have written.

    The runner reads its result from the path it puts in BLITZE_RESULT_PATH, so
    a double for the child process has to produce a document there — that file
    is the contract, and nothing about the terminal output matters any more.
    """
    Path(environment["BLITZE_RESULT_PATH"]).write_text(
        json.dumps({"playbook": "edge.yml", "hosts": hosts}), encoding="utf-8"
    )


class FakeRunnerResult:
    def __init__(self, *, rc: int = 0, status: str | None = None) -> None:
        self.rc = rc
        self.status = status or ("successful" if rc == 0 else "failed")


def _runner_result(
    arguments: dict[str, object],
    *,
    rc: int = 0,
    status: str | None = None,
    output: bytes = b"",
) -> FakeRunnerResult:
    artifact = Path(str(arguments["artifact_dir"])) / str(arguments["ident"])
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "stdout").write_bytes(output)
    return FakeRunnerResult(rc=rc, status=status)


def _runner_command(arguments: dict[str, object]) -> list[str]:
    return [
        str(arguments["binary"]),
        *shlex.split(str(arguments["cmdline"])),
        "--inventory",
        str(arguments["inventory"]),
        "--limit",
        str(arguments["limit"]),
    ]


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


def test_the_lock_is_held_against_a_genuinely_separate_process(settings):
    """`flock` is per open file description, and the fleet has several writers.

    The check above uses two descriptors in one process, which is the same
    mechanism but not the same claim. What the lock has to guarantee is that a
    CLI deploy, an API worker and a systemd timer cannot converge at once, and
    those are separate processes — so one of them is a real subprocess here.
    """
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(PROJECT_SRC)!r})",
            "from pathlib import Path",
            "from blitzecdn.infrastructure.ansible import DeploymentLock",
            "from blitzecdn.exceptions import DeploymentBusyError",
            "try:",
            f"    with DeploymentLock(Path({str(settings.deployment_lock_path)!r})):",
            "        print('acquired')",
            "except DeploymentBusyError:",
            "    print('busy')",
        ]
    )

    with ansible.DeploymentLock(settings.deployment_lock_path):
        held = subprocess.run(  # noqa: S603 - fixed argv built here
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
    assert held.stdout.strip() == "busy", held.stderr

    released = subprocess.run(  # noqa: S603 - fixed argv built here
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert released.stdout.strip() == "acquired", released.stderr


def test_runner_builds_a_check_command_and_keeps_the_raw_log(settings, monkeypatch):
    """Output goes to a log file; the result comes from the callback document."""
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    captured = []
    invocation: dict[str, object] = {}

    def fake_run(**kwargs):
        invocation.update(kwargs)
        captured.extend(_runner_command(kwargs))
        _callback_result(kwargs["envvars"], [{"host": "edge-a", "ok": 4, "changed": 1}])
        return _runner_result(
            kwargs, output=b"TASK [something] ***\nchanged: [edge-a]\n"
        )

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = runner.run(check=True)

    assert "--check" in captured and "--diff" in captured
    assert [host.host for host in run.hosts] == ["edge-a"]
    assert run.hosts[0].changed == 1
    assert invocation["suppress_env_files"] is True
    assert invocation["settings"] == {"runner_mode": "subprocess"}
    assert invocation["timeout"] == settings.deployment_timeout_seconds
    assert Path(run.log_path).read_text(encoding="utf-8").startswith("TASK [")


def test_the_result_document_is_removed_once_it_has_been_read(settings, monkeypatch):
    """It is working state: what survives is the parsed result and the log."""
    seen: list[Path] = []

    def fake_run(**kwargs):
        seen.append(Path(kwargs["envvars"]["BLITZE_RESULT_PATH"]))
        _callback_result(kwargs["envvars"], [{"host": "edge-a"}])
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=True)

    assert seen and not seen[0].exists()


def test_each_run_gets_a_result_path_of_its_own(settings, monkeypatch):
    """Purge, stats and decommission all skip the lock, so runs overlap."""
    paths: list[str] = []

    def fake_run(**kwargs):
        paths.append(kwargs["envvars"]["BLITZE_RESULT_PATH"])
        _callback_result(kwargs["envvars"], [{"host": "edge-a"}])
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    runner.run(check=True)
    runner.run(check=True)

    assert len(set(paths)) == 2


def test_a_run_that_reports_nothing_says_where_to_look(settings, monkeypatch):
    """Ansible died before reporting: there is no host data, only the log."""

    def fake_run(**kwargs):
        return _runner_result(
            kwargs, rc=4, output=b"ERROR! the inventory could not be read\n"
        )

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=False)

    assert run.status is RunStatus.FAILED
    assert run.reported is False
    assert run.hosts == ()
    assert run.log_path is not None and run.log_path in (run.error or "")


def test_an_unparseable_result_is_treated_as_no_result(settings, monkeypatch):
    """A half-written document must not become half-believed host data."""

    def fake_run(**kwargs):
        Path(kwargs["envvars"]["BLITZE_RESULT_PATH"]).write_text(
            '{"hosts": [', encoding="utf-8"
        )
        return _runner_result(kwargs, rc=2)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=False)

    assert run.reported is False
    assert run.status is RunStatus.FAILED


def test_run_logs_are_pruned_to_the_retention_limit(settings, monkeypatch):
    """One log per invocation, and the drift timer alone fires on a schedule."""
    bounded = settings.model_copy(update={"run_log_retention": 10})

    def fake_run(**kwargs):
        _callback_result(kwargs["envvars"], [{"host": "edge-a"}])
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    runner = ansible.AnsibleRunner(bounded, FakeEdgeStore())
    for _ in range(14):
        runner.run(check=True)

    assert len(list(bounded.log_dir.glob("*.log"))) == 10


def test_each_run_uses_an_isolated_ssh_control_path(settings, monkeypatch):
    control_paths: list[str] = []

    def fake_run(**kwargs):
        control_paths.append(kwargs["envvars"]["ANSIBLE_SSH_CONTROL_PATH_DIR"])
        assert Path(control_paths[-1]).is_dir()
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    runner.run(check=True)
    runner.run(check=True)

    assert len(set(control_paths)) == 2
    assert all(not Path(path).exists() for path in control_paths)


def test_runner_maps_its_timeout_status_to_the_domain(settings, monkeypatch):
    def fake_run(**kwargs):
        return _runner_result(kwargs, rc=254, status="timeout")

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=False)

    assert run.status is RunStatus.TIMED_OUT
    assert run.return_code == 124


def test_runner_translates_os_errors(settings, monkeypatch):
    def fail(**_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(ansible.ansible_runner, "run", fail)
    with pytest.raises(ExecutionError, match="unable to execute"):
        ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=False)


def test_runner_validates_required_paths(settings):
    broken = settings.model_copy(
        update={"inventory_path": settings.project_dir / "missing"}
    )
    with pytest.raises(ConfigurationError, match="inventory"):
        ansible.AnsibleRunner(broken, FakeEdgeStore()).validate(
            broken.generated_vars_path
        )


# Runner 2.4.3 (the latest release) still uses codecs.open internally. Keep the
# exception on this integration boundary only so all first-party warnings fail.
@pytest.mark.filterwarnings(
    r"ignore:codecs\.open\(\) is deprecated\. Use open\(\) instead\.:DeprecationWarning"
)
def test_real_ansible_runner_executes_a_syntax_check(settings, tmp_path):
    """Exercise the installed Runner boundary, not only its unit-test double."""
    executable = shutil.which("ansible-playbook")
    if executable is None:
        pytest.skip("ansible-playbook is not installed")
    variables = tmp_path / "desired-state.yml"
    variables.write_text("{}\n", encoding="utf-8")
    configured = settings.model_copy(update={"ansible_playbook": executable})

    run = ansible.AnsibleRunner(configured, FakeEdgeStore()).validate(variables)

    assert run.status is RunStatus.SUCCEEDED
    assert run.return_code == 0
    assert run.log_path is not None and Path(run.log_path).is_file()


@pytest.mark.filterwarnings(
    r"ignore:codecs\.open\(\) is deprecated\. Use open\(\) instead\.:DeprecationWarning"
)
def test_real_ansible_runner_keeps_the_structured_callback(settings, tmp_path):
    """Runner's event callback must not displace the domain-result callback."""
    executable = shutil.which("ansible-playbook")
    if executable is None:
        pytest.skip("ansible-playbook is not installed")
    callback_dir = PROJECT_SRC.parent / "ansible/plugins/callback"
    (settings.ansible_dir / "ansible.cfg").write_text(
        "[defaults]\n"
        f"callback_plugins = {callback_dir}\n"
        "callbacks_enabled = blitzecdn_result\n",
        encoding="utf-8",
    )
    settings.inventory_path.write_text(
        "all:\n  children:\n    blitzecdn_edges:\n      hosts:\n"
        "        edge-local:\n          ansible_connection: local\n",
        encoding="utf-8",
    )
    settings.playbook_path.write_text(
        "- hosts: blitzecdn_edges\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - ansible.builtin.debug:\n"
        "        msg: callback integration\n",
        encoding="utf-8",
    )
    settings.generated_vars_path.parent.mkdir(parents=True, exist_ok=True)
    settings.generated_vars_path.write_text("{}\n", encoding="utf-8")
    configured = settings.model_copy(update={"ansible_playbook": executable})

    run = ansible.AnsibleRunner(configured, FakeEdgeStore()).run(check=True)

    assert [host.host for host in run.hosts] == ["edge-local"]
    assert run.hosts[0].succeeded is True


def test_runner_builds_acme_challenge_command(settings, monkeypatch):
    settings.acme_challenge_playbook_path.write_text(
        "- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8"
    )
    captured = []

    def fake_run(**kwargs):
        captured.extend(_runner_command(kwargs))
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    result = ansible.AnsibleRunner(settings, FakeEdgeStore()).run_acme_challenge(
        action="present",
        domain="cdn.example.com",
        token="safe_token",  # noqa: S106 -- public ACME challenge token
        validation="safe.validation",
    )
    assert result.status is RunStatus.SUCCEEDED
    assert str(settings.acme_challenge_playbook_path) in captured
    assert "safe_token" not in captured


def _with_edges(*names: str) -> FakeEdgeStore:
    """A fleet for the runner to expand a `--limit` against.

    It used to be a YAML file this helper wrote, because the runner read the
    inventory to answer the same question. It now reads the same rows Ansible
    will be given, so a test supplies a store rather than a file — and there is
    no longer a way for the two to disagree about which edges exist.
    """
    return FakeEdgeStore(
        [
            edge(name, host=f"198.51.100.{index}")
            for index, name in enumerate(names, start=1)
        ]
    )


def test_a_run_without_a_limit_targets_the_whole_edge_group(settings):
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    assert runner._limit(None) == "blitzecdn_edges"
    assert runner._limit("  ") == "blitzecdn_edges"


def test_a_limit_resolves_to_the_matching_edges(settings):
    runner = ansible.AnsibleRunner(settings, _with_edges("edge-a", "edge-b", "other"))
    assert runner._limit("edge-a") == "edge-a"
    assert runner._limit("edge-a,other") == "edge-a,other"
    assert runner._limit("edge-*") == "edge-a,edge-b"


def test_a_run_records_the_edges_it_aimed_at(settings):
    """`hosts` alone cannot say what a stopped play never got to.

    A limit resolves to names here, from the same rows Ansible is handed, so
    the run carries what it targeted rather than leaving the reader to
    reconstruct it from an inventory that may have changed since.
    """
    runner = ansible.AnsibleRunner(settings, _with_edges("edge-a", "edge-b", "other"))
    assert runner._targeted(None) == ("edge-a", "edge-b", "other")
    assert runner._targeted("edge-*") == ("edge-a", "edge-b")


def test_a_limit_cannot_reach_a_host_outside_the_edge_group(settings):
    """The whole point of resolving against the inventory rather than passing
    a pattern through: an unknown name is refused, not silently targeted."""
    runner = ansible.AnsibleRunner(settings, _with_edges("edge-a"))
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
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    with pytest.raises((ConfigurationError, ValueError)):
        runner._limit(pattern)


def test_the_limit_reaches_the_ansible_command_line(settings, monkeypatch):
    (settings.ansible_dir / "playbooks/edge.yml").write_text("---\n", encoding="utf-8")
    settings.generated_vars_path.parent.mkdir(parents=True, exist_ok=True)
    settings.generated_vars_path.write_text("---\n", encoding="utf-8")
    captured: list[list[str]] = []

    def fake_run(**kwargs):
        captured.append(_runner_command(kwargs))
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    monkeypatch.setattr(
        ansible.shutil, "which", lambda _name: "/usr/bin/ansible-playbook"
    )
    ansible.AnsibleRunner(settings, _with_edges("edge-a")).run(
        check=True, host_limit="edge-a"
    )

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
    def fake_run(**kwargs):
        Path(kwargs["envvars"]["BLITZE_RESULT_PATH"]).write_text(
            json.dumps(_RESULT), encoding="utf-8"
        )
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=True)

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

    def fake_run(**kwargs):
        Path(kwargs["envvars"]["BLITZE_RESULT_PATH"]).write_text(
            json.dumps(_RESULT), encoding="utf-8"
        )
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=True)

    assert [change.task for change in run.host("edge-a").changes] == [
        "Render managed sites",
        "Enable desired sites",
    ]
    assert "No route to host" in run.summary()


def test_a_role_payload_arrives_on_the_host_that_published_it(settings, monkeypatch):
    """`blitzecdn_report` is how a role returns data rather than an outcome."""

    def fake_run(**kwargs):
        Path(kwargs["envvars"]["BLITZE_RESULT_PATH"]).write_text(
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
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    run = ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=True)

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

    def fake_run(**kwargs):
        captured.update(kwargs["envvars"])
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    ansible.AnsibleRunner(configured, FakeEdgeStore()).run(check=True)

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

    def fake_run(**kwargs):
        captured.extend(_runner_command(kwargs))
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    ansible.AnsibleRunner(configured, FakeEdgeStore()).run(check=True)

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

    def fake_run(**kwargs):
        captured.update(kwargs["envvars"])
        return _runner_result(kwargs)

    monkeypatch.setattr(ansible.ansible_runner, "run", fake_run)
    ansible.AnsibleRunner(settings, FakeEdgeStore()).run(check=True)

    assert captured["BLITZE_MAXMIND_LICENSE_KEY"] == ""
