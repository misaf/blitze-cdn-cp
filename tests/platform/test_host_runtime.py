"""Exercise SDK lifecycle boundaries without a Docker daemon or host writes."""

from __future__ import annotations

import json
import shutil
from unittest.mock import MagicMock

import docker
import jinja2
import pytest
import yaml
from paths import CORE_ANSIBLE

from blitzecdn import host_runtime
from blitzecdn.host_runtime import HostRuntime, HostRuntimeError, ServiceDefinitions


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    role = CORE_ANSIBLE / "roles/blitzecdn_controlplane"
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    defaults.update(
        blitzecdn_controlplane_install_dir=str(tmp_path),
        blitzecdn_controlplane_config_dir=str(tmp_path),
        blitzecdn_controlplane_state_dir=str(tmp_path / "state"),
    )
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701
    environment.filters["to_json"] = json.dumps
    source = environment.from_string(
        (role / "templates/compose.yml.j2").read_text()
    ).render(**defaults)
    service_file = tmp_path / "compose.yml"
    service_file.write_text(source)
    (tmp_path / "blitzecdn.env").write_text('BLITZE_API_KEYS="operator:original"\n')
    (tmp_path / "blitzecdn.toml").write_text("[blitzecdn]\nallow_empty_sites = false\n")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(host_runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(host_runtime.os, "fchown", lambda *args: None)
    client = MagicMock()
    runner = HostRuntime(client, ServiceDefinitions(service_file))
    return runner


def container(name):
    result = MagicMock()
    result.name = name
    result.labels = {host_runtime.SERVICE_LABEL: name}
    result.attrs = {"State": {"Status": "running", "Health": {"Status": "healthy"}}}
    return result


@pytest.mark.parametrize(
    "failure", [None, "discovery", "stop", "command", "recover", "publish", "interrupt"]
)
def test_restore_recovers_every_prior_writer_and_cleans_staging(
    runtime, monkeypatch, failure
):
    api, worker = container("blitzecdn-api"), container("blitzecdn-worker")
    events = []
    stages = []

    def discover(service):
        events.append("discover:" + service)
        if failure == "discovery":
            raise docker.errors.APIError("discovery failed")
        return [api] if service == "blitzecdn-api" else [worker]

    monkeypatch.setattr(runtime, "containers", discover)

    def stop(**kwargs):
        events.append("stop")
        if failure == "stop":
            raise docker.errors.APIError("stop failed")

    api.stop.side_effect = stop
    worker.stop.side_effect = stop

    def run(arguments, options):
        events.append("run")
        stage = next(
            host_runtime.Path(source)
            for source, mount in options["volumes"].items()
            if mount["bind"] == "/run/blitzecdn-backup"
        )
        stages.append(stage)
        assert "operator:original" in (stage / "blitzecdn.env").read_text()
        assert options["environment"]["COMPOSE_RESTORE_OFFLINE"] == "1"
        (stage / "blitzecdn.env").write_text("BLITZE_API_KEYS=operator:restored\n")
        (stage / "blitzecdn.toml").write_text("[blitzecdn]\nallow_empty_sites = true\n")
        if failure == "interrupt":
            raise KeyboardInterrupt
        return 42 if failure == "command" else 0

    monkeypatch.setattr(runtime, "run_command", run)

    def recover(previous):
        events.append("recover")
        assert previous == [api, worker]
        if failure == "recover":
            raise HostRuntimeError("recovery failed")

    monkeypatch.setattr(runtime, "recover", recover)
    if failure == "publish":
        monkeypatch.setattr(
            runtime,
            "publish_configuration",
            MagicMock(side_effect=OSError("publish failed")),
        )
    if failure in {"discovery", "stop", "recover", "publish", "interrupt"}:
        with pytest.raises(
            (docker.errors.APIError, HostRuntimeError, OSError, KeyboardInterrupt)
        ):
            runtime.execute(["backup", "restore", "archive", "--yes"])
    else:
        assert runtime.execute(["backup", "restore", "archive", "--yes"]) == (
            42 if failure == "command" else 0
        )
    assert events[-1] == (
        "discover:blitzecdn-api" if failure == "discovery" else "recover"
    )
    if failure in {"discovery", "stop"}:
        assert "run" not in events
    published = failure in {None, "recover"}
    target = runtime.configuration_paths()["blitzecdn.env"]
    assert ("restored" in target.read_text()) == published
    assert not published or target.stat().st_mode & 0o777 == 0o600
    assert all(not stage.exists() for stage in stages)


def test_database_only_restore_preserves_configuration_inode(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "containers", lambda service: [])
    monkeypatch.setattr(runtime, "run_command", lambda *args: 0)
    target = runtime.configuration_paths()["blitzecdn.toml"]
    inode = target.stat().st_ino
    assert runtime.execute(["backup", "restore", "archive", "--yes"]) == 0
    assert target.stat().st_ino == inode
    runtime.client.containers.create.assert_not_called()


@pytest.mark.parametrize("failure", [None, "command", "publish", "rollback"])
def test_restore_checkpoint_covers_host_publication_and_failed_recovery(
    runtime, monkeypatch, failure
):
    api, worker = container("blitzecdn-api"), container("blitzecdn-worker")
    monkeypatch.setattr(
        runtime,
        "containers",
        lambda service, **kwargs: [api if service == "blitzecdn-api" else worker],
    )
    targets = runtime.configuration_paths()
    original = {name: path.read_bytes() for name, path in targets.items()}
    checkpoint = targets["blitzecdn.env"].parent / "state/restore-recovery"
    state = {"database": "original"}
    calls = []

    def command(arguments, options):
        action = arguments[1]
        calls.append(action)
        stage = next(
            host_runtime.Path(source)
            for source, mount in options["volumes"].items()
            if mount["bind"] == "/run/blitzecdn-backup"
        )
        if action == "restore":
            checkpoint.mkdir()
            state["database"] = "restored"
            (stage / "blitzecdn.toml").write_text(
                "[blitzecdn]\nallow_empty_sites = true\n"
            )
            (stage / "blitzecdn.env").write_text("BLITZE_API_KEYS=operator:restored\n")
            return 42 if failure == "command" else 0
        if action == "recover":
            if failure == "rollback":
                return 42
            state["database"] = "original"
            for name, content in original.items():
                (stage / name).write_bytes(content)
        else:
            assert action == "commit-restore"
            shutil.rmtree(checkpoint)
        return 0

    monkeypatch.setattr(runtime, "run_command", command)
    replace = host_runtime.os.replace
    failed = []

    def fail_second_publication(source, destination):
        if (
            failure in {"publish", "rollback"}
            and destination == targets["blitzecdn.env"]
            and not failed
        ):
            failed.append(True)
            raise OSError("configuration publication failed")
        replace(source, destination)

    monkeypatch.setattr(host_runtime.os, "replace", fail_second_publication)
    recovered = []

    def restart(previous):
        assert previous == [api, worker]
        if failure:
            assert state["database"] == "original"
            assert {
                name: path.read_bytes() for name, path in targets.items()
            } == original
        recovered.extend(previous)

    monkeypatch.setattr(runtime, "recover", restart)
    if failure in {"publish", "rollback"}:
        with pytest.raises((OSError, HostRuntimeError)):
            runtime.execute(["backup", "restore", "archive", "--yes"])
    else:
        assert runtime.execute(["backup", "restore", "archive", "--yes"]) == (
            42 if failure == "command" else 0
        )
    if failure == "rollback":
        assert recovered == []
        assert checkpoint.exists()
        # An explicit retry recovers the stopped services too.
        failure = "command"
        assert runtime.execute(["backup", "recover"]) == 0
        assert recovered == [api, worker]
        assert not checkpoint.exists()
    else:
        assert recovered == [api, worker]
        assert not checkpoint.exists()
        assert calls == (
            ["restore", "commit-restore"]
            if failure is None
            else ["restore", "recover", "commit-restore"]
        )


def test_restore_refuses_a_concurrent_host_operation(runtime, monkeypatch):
    lock_path = (
        runtime.configuration_paths()["blitzecdn.env"].parent / "state/.restore.lock"
    )
    with lock_path.open("a+b") as lock:
        host_runtime.fcntl.flock(lock.fileno(), host_runtime.fcntl.LOCK_EX)
        with pytest.raises(HostRuntimeError, match="already running"):
            runtime.execute(["backup", "restore", "archive", "--yes"])
    runtime.client.containers.create.assert_not_called()


def test_restore_captures_configuration_under_the_host_lock(runtime, monkeypatch):
    lock_path = (
        runtime.configuration_paths()["blitzecdn.env"].parent / "state/.restore.lock"
    )
    capture = runtime.stage_configuration
    captured = []

    def capture_under_lock(stage, options):
        with (
            lock_path.open("a+b") as competing,
            pytest.raises(BlockingIOError),
        ):
            host_runtime.fcntl.flock(
                competing.fileno(),
                host_runtime.fcntl.LOCK_EX | host_runtime.fcntl.LOCK_NB,
            )
        captured.append(stage)
        capture(stage, options)

    monkeypatch.setattr(runtime, "stage_configuration", capture_under_lock)
    monkeypatch.setattr(runtime, "containers", lambda service: [])
    monkeypatch.setattr(runtime, "run_command", lambda *args: 0)
    assert runtime.execute(["backup", "restore", "archive", "--yes"]) == 0
    assert len(captured) == 1


def test_recovery_uses_restored_environment_and_attempts_worker_after_api_failure(
    runtime,
):
    api, worker = container("blitzecdn-api"), container("blitzecdn-worker")
    runtime.configuration_paths()["blitzecdn.env"].write_text(
        "BLITZE_API_KEYS=operator:new\n"
    )
    replacement = container("blitzecdn-worker")
    runtime.client.containers.create.side_effect = [
        docker.errors.APIError("create failed"),
        replacement,
    ]
    with pytest.raises(HostRuntimeError, match="create failed"):
        runtime.recover([api, worker])
    api.remove.assert_called_once_with(force=True)
    worker.remove.assert_called_once_with(force=True)
    replacement.start.assert_called_once()
    replacement.reload.assert_called_once()
    for call in runtime.client.containers.create.call_args_list:
        assert call.kwargs["environment"]["BLITZE_API_KEYS"] == "operator:new"
        assert call.kwargs["network_mode"] == "host"
        assert call.kwargs["restart_policy"] == {"Name": "unless-stopped"}
        assert call.kwargs["healthcheck"]["interval"] == 10_000_000_000
        assert call.kwargs["security_opt"] == ["no-new-privileges:true"]


@pytest.mark.parametrize("state", ["unhealthy", "exited", "starting"])
def test_health_wait_rejects_failed_and_timed_out_services(runtime, state):
    api = container("blitzecdn-api")
    if state == "exited":
        api.attrs["State"]["Status"] = state
    else:
        api.attrs["State"]["Health"]["Status"] = state
    with pytest.raises(HostRuntimeError):
        runtime.wait_healthy([api], timeout=0)


def test_discovery_is_scoped_to_project_service_and_persistent_containers(runtime):
    runtime.containers("blitzecdn-api")
    runtime.client.containers.list.assert_called_once_with(
        all=False,
        filters={
            "label": [
                "com.docker.compose.project=blitzecdn-control-plane",
                "com.docker.compose.service=blitzecdn-api",
                "com.docker.compose.oneoff=False",
            ]
        },
    )


def test_unknown_service_settings_fail_before_docker_mutations(runtime):
    document = runtime.definitions.document
    document["services"]["blitzecdn-api"]["privileged"] = True
    runtime.definitions.path.write_text(yaml.safe_dump(document))
    with pytest.raises(HostRuntimeError, match="Unsupported service settings"):
        ServiceDefinitions(runtime.definitions.path)
    runtime.client.containers.create.assert_not_called()


def test_a_normal_command_starts_nothing_and_forwards_the_environment(
    runtime, monkeypatch
):
    """No service has to be brought up before a disposable CLI container runs.

    One did: the broker its command would have talked to. Durable work is rows
    in the state volume this container already mounts, so the whole
    dependency-starting path went with it.
    """
    monkeypatch.setenv("BLITZE_ALLOW_EMPTY_SITES", "1")
    command = MagicMock(return_value=17)
    monkeypatch.setattr(runtime, "run_command", command)
    assert runtime.execute(["doctor"]) == 17
    runtime.client.containers.create.assert_not_called()
    assert command.call_args.args[1]["environment"]["BLITZE_ALLOW_EMPTY_SITES"] == "1"
    assert "COMPOSE_RESTORE_OFFLINE" not in command.call_args.args[1]["environment"]


def test_disposable_command_preserves_exit_code_and_removes_container(
    runtime, monkeypatch, capsys
):
    child = runtime.client.containers.create.return_value
    child.attach.return_value = [(b"output\n", b"error\n")]
    child.wait.return_value = {"StatusCode": 19}
    monkeypatch.setattr(host_runtime, "forward_input", lambda *args: None)
    assert (
        runtime.run_command(["doctor"], runtime.definitions.options("blitzecdn-cli"))
        == 19
    )
    captured = capsys.readouterr()
    assert captured.out == "output\n"
    assert captured.err == "error\n"
    child.remove.assert_called_once_with(force=True)


def test_stdin_socket_forwards_bytes_and_eof(tmp_path, monkeypatch):
    import socket
    import threading
    from types import SimpleNamespace

    source = tmp_path / "stdin"
    source.write_bytes(b"yes\n")
    with source.open("rb") as stdin:
        monkeypatch.setattr(host_runtime.sys, "stdin", stdin)
        sender, receiver = socket.socketpair()
        try:
            host_runtime.forward_input(SimpleNamespace(_sock=sender), threading.Event())
            assert receiver.recv(100) == b"yes\n"
            assert receiver.recv(100) == b""
        finally:
            sender.close()
            receiver.close()


def test_launcher_passes_application_flags_and_closes_client(runtime, monkeypatch):
    monkeypatch.setattr(
        host_runtime.sys,
        "argv",
        [
            "host_runtime.py",
            "--services",
            str(runtime.definitions.path),
            "--",
            "--help",
        ],
    )
    monkeypatch.setattr(host_runtime.signal, "signal", lambda *args: None)
    factory = MagicMock(return_value=runtime.client)
    monkeypatch.setattr(host_runtime.docker, "DockerClient", factory)
    execute = MagicMock(return_value=0)
    monkeypatch.setattr(HostRuntime, "execute", execute)
    assert host_runtime.main() == 0
    execute.assert_called_once_with(["--help"])
    factory.assert_called_once_with(
        base_url="unix:///var/run/docker.sock", version="auto"
    )
    runtime.client.close.assert_called_once()
