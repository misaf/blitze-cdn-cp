"""Test engine for the real host wrapper. Invoked only by test_host_runtime."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

from blitzecdn.host import compose_files


def restore(stage, state, checkpoint, failure, arguments):
    assert "COMPOSE_RESTORE_OFFLINE=1" in arguments
    if failure == "restore":
        return 42
    checkpoint.mkdir()
    for filename in ("blitzecdn.toml", "blitzecdn.env"):
        (checkpoint / filename).write_bytes((stage / filename).read_bytes())
        (stage / filename).write_text(
            'BLITZE_API_KEYS="operator:restored$literal"\n'
            if filename.endswith(".env")
            else "restored"
        )
    (checkpoint / "database").write_bytes((state / "database").read_bytes())
    (state / "database").write_text("restored")
    if failure == "disconnect":
        return 1
    if failure == "interrupt":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        (state / "waiting").touch()
        while True:
            time.sleep(1)


def invocation(arguments):
    event = {"arguments": arguments}
    if "blitzecdn.host.compose_files" in arguments:
        position = arguments.index("--config-dir") + 2
        action = arguments[position]
    elif arguments[:1] in (["rm"], ["ps"]):
        action = arguments[0]
    else:
        assert arguments[:2] == ["compose", "--file"]
        action = arguments[arguments.index("--project-name") + 2]
        if action == "run":
            position = arguments.index("blitzecdn-cli") + 1
            command = arguments[position:]
            action = (
                command[command.index("backup") + 1]
                if "--volume" in arguments
                and "backup" in command
                and "--help" not in command
                else "normal"
            )
            if "--volume" in arguments:
                stage = Path(arguments[arguments.index("--volume") + 1].split(":")[0])
                event["staged_environment"] = (stage / "blitzecdn.env").read_text()
                if action == "create":
                    event["input"] = sys.stdin.read()
            event["allow_empty_sites"] = os.environ.get("BLITZE_ALLOW_EMPTY_SITES")
            event["docker_host"] = os.environ.get("DOCKER_HOST")
            event["docker_context"] = os.environ.get("DOCKER_CONTEXT")
    event["action"] = action
    return event


def lock():
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 1
    return 0


def publish(config, stage, scenario, scenario_path):
    if scenario.get("failure") in {"publish", "rollback"} and not scenario.get(
        "publication_failed"
    ):
        (config / "blitzecdn.toml").write_text((stage / "blitzecdn.toml").read_text())
        scenario["publication_failed"] = True
        scenario_path.write_text(json.dumps(scenario))
        return 1
    compose_files.publish_configuration(config, stage)
    return 0


def recover(stage, state, checkpoint, failure, arguments):
    assert "COMPOSE_RESTORE_OFFLINE=1" in arguments
    if failure == "rollback":
        return 42
    for filename in ("blitzecdn.toml", "blitzecdn.env"):
        (stage / filename).write_bytes((checkpoint / filename).read_bytes())
    (state / "database").write_bytes((checkpoint / "database").read_bytes())


def lifecycle(action, failure, scenario):
    if action == "rm":
        return int(failure == "disconnect")
    if failure == action:
        return 1
    if action == "ps":
        print(
            "still-running"
            if failure == "disconnect"
            else "\n".join(scenario["running"])
        )
    return 0


def main():
    config = Path(os.environ["AUDIT_CONFIG"])
    state = Path(os.environ["AUDIT_STATE"])
    scenario_path = Path(os.environ["AUDIT_SCENARIO"])
    scenario = json.loads(scenario_path.read_text())
    failure = scenario.get("failure")
    arguments = sys.argv[2:]
    compose_files.os.chown = lambda *args: None
    compose_files.os.fchown = lambda *args: None
    event = invocation(arguments)
    action = event["action"]
    if "--volume" in arguments:
        stage = Path(arguments[arguments.index("--volume") + 1].split(":")[0])
    with Path(os.environ["AUDIT_EVENTS"]).open("a") as output:
        output.write(json.dumps(event) + "\n")
    checkpoint = state / "restore-recovery"
    if action == "environment":
        return compose_files.main(["--config-dir", str(config), "environment"])
    if action == "stage":
        compose_files.stage_configuration(config, Path(arguments[-1]))
    elif action == "publish":
        return publish(config, Path(arguments[-1]), scenario, scenario_path)
    elif action in {"ps", "stop", "up", "rm"}:
        return lifecycle(action, failure, scenario)
    elif action == "restore":
        return restore(stage, state, checkpoint, failure, arguments)
    elif action == "recover":
        return recover(stage, state, checkpoint, failure, arguments)
    elif action == "commit-restore":
        import shutil

        shutil.rmtree(checkpoint)
    elif action == "normal":
        return scenario.get("normal_status", 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(lock() if sys.argv[1] == "flock" else main())
