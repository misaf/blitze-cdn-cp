"""Run the installed host shell against a fake engine, including failure paths."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import jinja2
import pytest
import yaml
from support.paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.host import compose_files


@pytest.fixture
def host(tmp_path):
    config, state, runtime, binaries = [
        tmp_path / name for name in ("config", "state", "runtime", "bin")
    ]
    for directory in (config, state, runtime, binaries):
        directory.mkdir()
    (config / "blitzecdn.env").write_text(
        'BLITZE_API_KEYS="operator:original$literal"\n'
    )
    (config / "blitzecdn.toml").write_text("[blitzecdn]\nallow_empty_sites = false\n")
    (state / "database").write_text("original")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"running": ["blitzecdn-api", "blitzecdn-worker"]}))
    events = tmp_path / "events.jsonl"
    events.touch()
    fake = Path(__file__).with_name("fake_compose.py")
    for name in ("docker", "flock"):
        script = binaries / name
        script.write_text(
            f"#!/bin/sh\nexec {shlex.quote(os.sys.executable)} "
            f'{shlex.quote(str(fake))} {name} "$@"\n'
        )
        script.chmod(0o755)
    role = CORE_ANSIBLE / "roles/blitzecdn_controlplane"
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    defaults.update(
        blitzecdn_controlplane_config_dir=str(config),
        blitzecdn_controlplane_state_dir=str(state),
        blitzecdn_controlplane_runtime_dir=str(runtime),
    )
    template = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701
    template.filters["quote"] = shlex.quote
    wrapper = tmp_path / "blitzecdn"
    wrapper.write_text(
        template.from_string((role / "templates/blitzecdn-cli.j2").read_text())
        .render(**defaults)
        # The test engine cannot touch a host. Do not require sudo to exercise
        # the exact installed shell, including on a contributor's macOS host.
        .replace("if [[ ${EUID} -ne 0 ]]; then", "if false; then")
    )
    environment = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "AUDIT_CONFIG": str(config),
        "AUDIT_STATE": str(state),
        "AUDIT_SCENARIO": str(scenario),
        "AUDIT_EVENTS": str(events),
        "DOCKER_HOST": "tcp://wrong-host:2375",
        "DOCKER_CONTEXT": "wrong-context",
        "COMPOSE_PROJECT_NAME": "wrong-project",
    }
    return {
        "wrapper": wrapper,
        "config": config,
        "state": state,
        "runtime": runtime,
        "scenario": scenario,
        "events": events,
        "env": environment,
    }


def run(host, *arguments, **kwargs):
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(host["wrapper"]), *arguments],
        env=host["env"],
        text=True,
        capture_output=True,
        timeout=15,
        **kwargs,
    )


def events(host):
    return [json.loads(line) for line in host["events"].read_text().splitlines()]


def scenario(host, **updates):
    path = host["scenario"]
    path.write_text(json.dumps({**json.loads(path.read_text()), **updates}))


def actions(host):
    return [event["action"] for event in events(host)]


@pytest.mark.parametrize(
    "flags", [[], ["--verbose"], ["-v", "--log-json"], ["--no-log-json"]]
)
def test_restore_handles_global_options_and_recreates_original_writers(host, flags):
    scenario(host, running=["blitzecdn-worker"])
    result = run(host, *flags, "backup", "restore", "archive", "--yes")
    assert result.returncode == 0, result.stderr
    assert actions(host) == [
        "environment",
        "stage",
        "ps",
        "stop",
        "restore",
        "publish",
        "environment",
        "commit-restore",
        "up",
    ]
    assert (host["state"] / "database").read_text() == "restored"
    assert "restored" in (host["config"] / "blitzecdn.env").read_text()
    restart = events(host)[-1]["arguments"]
    assert restart[-1] == "blitzecdn-worker"
    assert "blitzecdn-api" not in restart
    assert "--force-recreate" in restart and "--wait" in restart
    assert restart[restart.index("--wait-timeout") + 1] == "180"
    assert not list(host["runtime"].iterdir())
    assert not (host["state"] / "restore-recovery").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["doctor"],
        ["backup", "restore", "--help"],
        ["--verbose", "backup", "recover", "--help"],
    ],
)
def test_normal_commands_and_help_never_stop_services(host, arguments):
    scenario(host, normal_status=19)
    result = run(host, *arguments)
    assert result.returncode == 19
    assert actions(host) == ["environment", "normal"]
    invocation = events(host)[-1]
    assert invocation["arguments"][-len(arguments) :] == arguments
    assert "--no-deps" in invocation["arguments"]
    assert invocation["docker_host"] == "unix:///var/run/docker.sock"
    assert invocation["docker_context"] is None
    assert (
        invocation["arguments"][invocation["arguments"].index("--project-name") + 1]
        == "blitzecdn-control-plane"
    )


def test_backup_create_with_global_flags_stages_secrets_and_preserves_input(host):
    host["env"]["BLITZE_ALLOW_EMPTY_SITES"] = "1"
    result = run(host, "--verbose", "backup", "create", input="yes\n")
    assert result.returncode == 0, result.stderr
    assert actions(host) == ["environment", "stage", "create"]
    invocation = events(host)[-1]
    assert invocation["input"] == "yes\n"
    assert (
        invocation["staged_environment"]
        == (host["config"] / "blitzecdn.env").read_text()
    )
    assert "COMPOSE_RESTORE_OFFLINE=1" not in invocation["arguments"]
    assert invocation["allow_empty_sites"] == "1"
    assert not list(host["runtime"].iterdir())


@pytest.mark.parametrize(
    "failure", ["ps", "stop", "restore", "publish", "rollback", "up"]
)
def test_restore_failures_preserve_recovery_order(host, failure):
    scenario(host, failure=failure)
    result = run(host, "backup", "restore", "archive", "--yes")
    assert result.returncode != 0
    recorded = actions(host)
    if failure == "ps":
        assert "stop" not in recorded and "up" not in recorded
    elif failure == "rollback":
        assert "recover" in recorded and "up" not in recorded
        assert (host["state"] / "restore-recovery").exists()
        assert "services remain stopped" in result.stderr
        scenario(host, failure=None)
        retry = run(host, "--verbose", "backup", "recover")
        assert retry.returncode == 0, retry.stderr
        assert actions(host)[-1] == "up"
        assert (host["state"] / "database").read_text() == "original"
        assert "original" in (host["config"] / "blitzecdn.env").read_text()
    elif failure == "publish":
        assert recorded.index("recover") < recorded.index("up")
        assert (host["state"] / "database").read_text() == "original"
        assert "original" in (host["config"] / "blitzecdn.env").read_text()
    else:
        assert recorded[-1] == "up"
    assert not list(host["runtime"].iterdir())


def test_unfinished_restore_is_refused_before_shutdown(host):
    (host["state"] / "restore-recovery").mkdir()
    result = run(host, "backup", "restore", "archive", "--yes")
    assert result.returncode == 1
    assert "Unfinished restore" in result.stderr
    assert actions(host) == ["environment"]


def test_concurrent_restore_is_refused(host):
    import fcntl

    with (host["state"] / ".restore.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(host, "backup", "restore", "archive", "--yes")
    assert result.returncode == 1
    assert "already running" in result.stderr
    assert "stage" not in actions(host)


@pytest.mark.parametrize(
    "received,expected", [(signal.SIGTERM, 143), (signal.SIGINT, 130)]
)
def test_interrupted_restore_recovers_before_restarting(host, received, expected):
    scenario(host, failure="interrupt")
    process = subprocess.Popen(  # noqa: S603
        ["/bin/bash", str(host["wrapper"]), "backup", "restore", "archive", "--yes"],
        env=host["env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (host["state"] / "waiting").exists():
            assert time.monotonic() < deadline
            assert process.poll() is None
            time.sleep(0.02)
        process.send_signal(received)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == expected, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    recorded = actions(host)
    assert recorded.index("rm") < recorded.index("recover") < recorded.index("up")
    assert (host["state"] / "database").read_text() == "original"
    assert not list(host["runtime"].iterdir())


def test_configuration_publication_preserves_unchanged_inode_and_modes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(compose_files.os, "fchown", lambda *args: None)
    config, stage = tmp_path / "config", tmp_path / "stage"
    config.mkdir()
    stage.mkdir()
    for filename in ("blitzecdn.toml", "blitzecdn.env"):
        (config / filename).write_text("original")
        (stage / filename).write_text("original")
    target = config / "blitzecdn.toml"
    inode = target.stat().st_ino
    compose_files.publish_configuration(config, stage)
    assert target.stat().st_ino == inode
    (stage / "blitzecdn.env").write_text("restored")
    compose_files.publish_configuration(config, stage)
    assert (config / "blitzecdn.env").read_text() == "restored"
    assert (config / "blitzecdn.env").stat().st_mode & 0o777 == 0o600
    assert target.stat().st_ino == inode


def test_environment_uses_literal_values_and_first_assignment(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_files.os, "fchown", lambda *args: None)
    (tmp_path / "blitzecdn.env").write_text(
        'export SECRET="value$UNSET \\"quoted\\""\nSECRET=ignored\nEMPTY=\n'
    )
    assert compose_files.environment(tmp_path)
    target = tmp_path / "compose.env"
    assert target.read_text() == 'SECRET=value$UNSET "quoted"\nEMPTY=\n'
    assert target.stat().st_mode & 0o777 == 0o600
    assert not compose_files.environment(tmp_path)


def test_installed_wrapper_passes_shellcheck(host):
    executable = Path(os.sys.executable).parent / "shellcheck"
    result = subprocess.run(  # noqa: S603
        [str(executable), str(host["wrapper"])],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_restore_does_not_start_services_that_did_not_exist(host):
    scenario(host, running=[])
    result = run(host, "backup", "restore", "archive", "--yes")
    assert result.returncode == 0, result.stderr
    assert "stop" not in actions(host) and "up" not in actions(host)
    assert "restored" in (host["config"] / "compose.env").read_text()


@pytest.mark.parametrize(
    "arguments", [[], ["--verbose"], ["--no-verbose", "backup", "restore", "archive"]]
)
def test_empty_or_invalid_commands_are_left_to_application_parser(host, arguments):
    result = run(host, *arguments)
    assert result.returncode == 0, result.stderr
    assert actions(host) == ["environment", "normal"]


def test_failed_command_cleanup_never_rolls_back_or_restarts_writers(host):
    scenario(host, failure="disconnect")
    result = run(host, "backup", "restore", "archive", "--yes")
    assert result.returncode == 1
    assert "Cannot confirm" in result.stderr
    assert "recover" not in actions(host) and "up" not in actions(host)
    assert (host["state"] / "restore-recovery").exists()
