"""Operational contracts for the Docker-owned control-plane runtime."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml
from paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.core.runtime import broker
from blitzecdn.docker import (
    CONTROL_PLANE_DOCKERFILE,
    CONTROL_PLANE_DOCKERIGNORE,
)

ROOT = REPO_ROOT
ROLE = CORE_ANSIBLE / "roles/blitzecdn_controlplane"
UNINSTALL = CORE_ANSIBLE / "roles/blitzecdn_uninstall/tasks/main.yml"


@pytest.mark.parametrize(
    ("scenario", "status", "operations"),
    [
        ("discovery_failure", 42, ["ps"]),
        ("partial_stop", 43, ["ps", "stop", "up"]),
        ("restore_failure", 44, ["ps", "stop", "run", "up"]),
        ("recovery_failure", 1, ["ps", "stop", "run", "up"]),
        ("restore", 0, ["ps", "stop", "run", "up"]),
        ("no_running", 0, ["ps", "run"]),
        ("create", 0, ["run"]),
        ("database_only", 0, ["ps", "stop", "run", "up"]),
    ],
)
def test_backup_wrapper_executes_safe_lifecycle(tmp_path, scenario, status, operations):
    """Run the real shell wrapper, replacing only Docker and root-only helpers."""
    project = tmp_path / "project"
    config = tmp_path / "config"
    scratch = tmp_path / "scratch"
    for path in (project, config, scratch):
        path.mkdir()
    configuration = project / "blitzecdn.toml"
    environment_file = config / "blitzecdn.env"
    configuration.write_text("[blitzecdn]\nallow_empty_sites = false\n")
    environment_file.write_text('BLITZE_API_KEY="original-secret"\n')
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701
    environment.filters["quote"] = shlex.quote
    wrapper = environment.from_string(
        (ROLE / "templates/blitzecdn-cli.j2").read_text()
    ).render(
        **{
            **_role_defaults(),
            "blitzecdn_controlplane_install_dir": str(project),
            "blitzecdn_controlplane_config_dir": str(config),
        }
    )
    # The test runs without sudo. The installed wrapper's privilege boundary
    # remains in place; only the test copy bypasses it.
    wrapper = wrapper.replace("if [[ ${EUID} -ne 0 ]]; then", "if false; then")
    script = tmp_path / "wrapper"
    script.write_text(wrapper)
    log = tmp_path / "calls"
    docker = tmp_path / "docker"
    docker.write_text(
        r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
operation = args[3]
with open(os.environ['CALL_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\n')
scenario = os.environ['SCENARIO']
if operation == 'ps':
    if scenario == 'discovery_failure':
        sys.exit(42)
    if scenario != 'no_running':
        print('blitzecdn-api\nblitzecdn-worker\nredis')
if operation == 'stop' and scenario == 'partial_stop':
    sys.exit(43)
if operation == 'up' and scenario == 'recovery_failure':
    sys.exit(45)
if operation == 'run':
    stage = pathlib.Path(args[args.index('--volume') + 1].split(':')[0])
    assert (stage / 'blitzecdn.env').read_text() == 'BLITZE_API_KEY="original-secret"\n'
    assert 'BLITZE_ENVIRONMENT_PATH=/run/blitzecdn-backup/blitzecdn.env' in args
    assert 'BLITZE_PROJECT_DIR=/run/blitzecdn-backup' in args
    if scenario != 'create':
        assert 'COMPOSE_RESTORE_OFFLINE=1' in args
    if scenario not in {'create', 'database_only'}:
        (stage / 'blitzecdn.toml').write_text('[blitzecdn]\nallow_empty_sites = true\n')
        (stage / 'blitzecdn.env').write_text('BLITZE_API_KEY="restored-secret"\n')
    if scenario == 'restore_failure':
        sys.exit(44)
"""
    )
    # Simulate root ownership changes without requiring privileged tests.
    chown = tmp_path / "chown"
    chown.write_text("#!/bin/sh\nexit 0\n")
    install = tmp_path / "install"
    install.write_text(
        '#!/bin/sh\nif [ "$1" = -o ]; then shift 4; fi\nexec /usr/bin/install "$@"\n'
    )
    for executable in (docker, chown, install):
        executable.chmod(0o755)
    original_inode = configuration.stat().st_ino
    result = subprocess.run(  # noqa: S603 - trusted generated wrapper and fake commands
        [
            "/bin/bash",
            str(script),
            "backup",
            "create" if scenario == "create" else "restore",
            "archive",
            "--yes",
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TMPDIR": str(scratch),
            "CALL_LOG": str(log),
            "SCENARIO": scenario,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == status, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[3] for call in calls] == operations
    for call in calls:
        if call[3] in {"stop", "up"}:
            assert call[-2:] == ["blitzecdn-api", "blitzecdn-worker"]
        if call[3] == "up":
            assert "--no-deps" in call
            assert "--force-recreate" in call
            assert "--wait" in call
    published = scenario in {"restore", "no_running", "recovery_failure"}
    assert ("restored-secret" in environment_file.read_text()) == published
    assert ("true" in configuration.read_text()) == published
    if scenario == "database_only":
        assert configuration.stat().st_ino == original_inode
    assert list(scratch.iterdir()) == []


def _role_defaults() -> dict[str, Any]:
    """The role's own `defaults/main.yml`.

    Read rather than restated, so a variable the template needs is proved
    to have a default that renders — the failure otherwise is a
    `StrictUndefined` on a controller mid-install, not here.
    """
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))


def _compose() -> dict[str, Any]:
    environment = jinja2.Environment(  # noqa: S701 - renders YAML, not HTML
        undefined=jinja2.StrictUndefined
    )
    environment.filters["to_json"] = json.dumps
    rendered = environment.from_string(
        (ROLE / "templates/compose.yml.j2").read_text(encoding="utf-8")
    ).render(
        blitzecdn_controlplane_compose_project="blitzecdn-control-plane",
        blitzecdn_controlplane_image="blitzecdn-control-plane:test",
        blitzecdn_controlplane_install_dir="/opt/blitzecdn",
        blitzecdn_controlplane_config_dir="/etc/blitzecdn",
        blitzecdn_controlplane_state_dir="/var/lib/blitzecdn",
        blitzecdn_controlplane_backup_dir="/var/backups/blitzecdn",
        blitzecdn_controlplane_redis_image="redis:test",
        blitzecdn_controlplane_dockerfile=_role_defaults()[
            "blitzecdn_controlplane_dockerfile"
        ],
    )
    return yaml.safe_load(rendered)


def test_control_plane_uses_a_project_name_distinct_from_the_edge_stack():
    assert _compose()["name"] == "blitzecdn-control-plane"


def test_api_worker_and_redis_are_dedicated_persistent_services():
    services = _compose()["services"]
    assert set(services) == {
        "redis",
        "blitzecdn-api",
        "blitzecdn-worker",
        "blitzecdn-cli",
    }
    assert services["blitzecdn-api"]["command"][0] == "uvicorn"
    assert services["blitzecdn-worker"]["command"][0] == "dramatiq"
    assert services["blitzecdn-api"]["restart"] == "unless-stopped"
    assert services["blitzecdn-worker"]["restart"] == "unless-stopped"
    assert "healthcheck" in services["blitzecdn-api"]
    assert "healthcheck" in services["blitzecdn-worker"]


def test_the_worker_consumes_every_queue_the_broker_publishes_to():
    """The third copy of the queue names, held against the first.

    ``infrastructure/broker.py`` names the queues, ``worker.py`` declares actors
    on them, and this Compose command tells Dramatiq which ones to consume. A
    queue missing from the command is not an error anywhere — the messages just
    sit in Redis unread — so the list is checked rather than trusted.
    """
    command = _compose()["services"]["blitzecdn-worker"]["command"]
    queues = command[command.index("--queues") + 1 : command.index("--processes")]

    assert set(queues) >= {broker.DEPLOYMENT_QUEUE, broker.SCHEDULED_QUEUE}


def test_cli_is_ephemeral_and_not_a_persistent_daemon():
    cli = _compose()["services"]["blitzecdn-cli"]
    assert cli["profiles"] == ["cli"]
    assert cli["restart"] == "no"
    assert cli["entrypoint"] == ["blitzecdn"]


def test_application_services_reuse_the_image_non_root_identity():
    services = _compose()["services"]
    for name in ("blitzecdn-api", "blitzecdn-worker", "blitzecdn-cli"):
        service = services[name]
        assert "user" not in service
        assert "group_add" not in service
        assert service["security_opt"] == ["no-new-privileges:true"]

    dockerfile = CONTROL_PLANE_DOCKERFILE.read_text(encoding="utf-8")
    assert "USER nobody:nogroup" in dockerfile
    for duplicate_account in ("useradd", "groupadd", "adduser", "addgroup"):
        assert duplicate_account not in dockerfile
    for dynamic_mapping in ("BLITZE_UID", "BLITZE_GID", "PUID", "PGID"):
        assert dynamic_mapping not in dockerfile
    dockerignore = CONTROL_PLANE_DOCKERIGNORE.read_text(encoding="utf-8")
    assert "blitzecdn.toml" in dockerignore.splitlines()


def test_control_plane_mounts_only_the_required_writable_state():
    services = _compose()["services"]
    shared = {
        "/var/lib/blitzecdn:/opt/blitzecdn/.state",
        "/opt/blitzecdn/blitzecdn.toml:/opt/blitzecdn/blitzecdn.toml:ro",
    }
    for name in ("blitzecdn-api", "blitzecdn-worker"):
        assert set(services[name]["volumes"]) == shared
        assert all("docker.sock" not in mount for mount in services[name]["volumes"])
    assert set(services["blitzecdn-cli"]["volumes"]) == shared | {
        "/var/backups/blitzecdn:/var/backups/blitzecdn"
    }


def test_host_wrapper_uses_compose_for_commands_and_offline_restore():
    wrapper = (ROLE / "templates/blitzecdn-cli.j2").read_text(encoding="utf-8")
    environment = jinja2.Environment(  # noqa: S701 - renders a shell script, not HTML
        undefined=jinja2.StrictUndefined
    )
    environment.filters["quote"] = str
    rendered = environment.from_string(wrapper).render(
        **{
            **_role_defaults(),
            "blitzecdn_controlplane_compose_file": "/etc/blitzecdn/control-plane.yml",
        }
    )

    assert "docker compose --file" in wrapper
    assert "run --rm" in wrapper
    assert "${#" not in wrapper
    assert "readonly compose_file=/etc/blitzecdn/control-plane.yml" in rendered
    assert 'stop "${running[@]}"' in wrapper
    assert (
        "up --detach --no-deps --force-recreate --wait "
        '--wait-timeout 180 "${running[@]}"' in wrapper
    )
    assert "COMPOSE_RESTORE_OFFLINE=1" in wrapper
    assert "docker exec" not in wrapper


def test_a_restore_does_not_report_success_over_a_control_plane_that_is_down():
    """Bringing the services back is not the same as them coming back.

    The wrapper stops the API and the worker so nothing holds the database
    open, and an EXIT trap starts them again — whether the restore succeeded
    or failed, which is the point of the trap. But `up --detach` returns as
    soon as the containers exist, so a database this image cannot open left
    the API crash-looping behind a `Restored: database` and a zero exit: the
    one moment an operator most needs to be told the truth.

    So the trap waits for health and fails loudly if it never arrives, and it
    ends with the status the restore itself exited with rather than the
    trap's own — an EXIT trap that returns normally leaves the original code
    in place, and a restore that failed must not be reported as a success
    because the containers restarted fine afterwards.
    """
    wrapper = (ROLE / "templates/blitzecdn-cli.j2").read_text(encoding="utf-8")
    body = wrapper.split("restore_running() {", 1)[1].split("\n  }", 1)[0]
    # Executable lines only. The comment above this very call explains why it
    # waits, and a test that reads the explanation passes over the code that
    # stopped doing it — which is what happened when this guard was written.
    trap = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )

    assert "--wait" in trap, (
        "the restore trap starts the control plane without waiting for it, so "
        "a restore the plane cannot open reports success"
    )
    assert "exit 1" in trap
    assert trap.rstrip().endswith('exit "${status}"'), (
        "the trap must end with the restore's own exit status, or a failed "
        "restore is reported as a success once the containers restart"
    )
    assert "local status=$?" in trap


def test_container_ssh_uses_the_mounted_controller_configuration():
    config = (CORE_ANSIBLE / "ansible.cfg").read_text(encoding="utf-8")
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    probe = next(
        task
        for task in tasks
        if task.get("name")
        == "Verify the container can reach this host as the deployment account"
    )
    controller_config = "/opt/blitzecdn/.state/.ssh/config"

    assert f"ssh_args = -F {controller_config} " in config
    argv = probe["ansible.builtin.command"]["argv"]
    assert argv[argv.index("-F") + 1] == (
        "{{ blitzecdn_controlplane_install_dir }}/.state/.ssh/config"
    )


def test_upgrade_recreates_containers_and_uninstall_removes_the_project():
    role_tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    recreate = next(
        task
        for task in role_tasks
        if task.get("name") == "Recreate and start the control-plane services"
    )
    assert recreate["ansible.builtin.command"]["argv"][-4:] == [
        "up",
        "--detach",
        "--remove-orphans",
        "--force-recreate",
    ]

    uninstall_tasks = yaml.safe_load(UNINSTALL.read_text(encoding="utf-8"))
    down = next(
        task
        for task in uninstall_tasks
        if task.get("name")
        == "Remove the control-plane containers and persistent Redis volume"
    )
    assert down["ansible.builtin.command"]["argv"][-3:] == [
        "down",
        "--volumes",
        "--remove-orphans",
    ]


def test_no_host_units_launch_application_daemons():
    unit_dir = ROOT / "packaging/systemd"
    assert not (unit_dir / "blitzecdn-api.service").exists()
    assert not (unit_dir / "blitzecdn-worker.service").exists()
    for path in (ROOT / "packaging").rglob("*.service"):
        text = path.read_text(encoding="utf-8").lower()
        assert "uvicorn" not in text
        assert "dramatiq" not in text


def test_host_has_no_blitzecdn_service_account_contract():
    sources = [
        ROLE / "defaults/main.yml",
        ROLE / "meta/argument_specs.yml",
        ROLE / "tasks/main.yml",
        CORE_ANSIBLE / "roles/blitzecdn_uninstall/defaults/main.yml",
        CORE_ANSIBLE / "roles/blitzecdn_uninstall/meta/argument_specs.yml",
        CORE_ANSIBLE / "roles/blitzecdn_uninstall/tasks/main.yml",
    ]
    document = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    for obsolete in (
        "blitzecdn_controlplane_service_user",
        "blitzecdn_controlplane_service_uid",
        "blitzecdn_controlplane_service_gid",
        "blitzecdn_uninstall_service_user",
        "become_user:",
        "sudo -u blitzecdn",
        "runuser",
    ):
        assert obsolete not in document

    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    users = [
        task["ansible.builtin.user"] for task in tasks if "ansible.builtin.user" in task
    ]
    assert users == [
        {
            "name": "{{ blitzecdn_controlplane_deploy_user }}",
            "shell": "/bin/bash",
            "create_home": True,
        }
    ]


def test_host_permissions_match_container_write_requirements():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))

    def file_task(name: str) -> dict[str, Any]:
        return next(
            task["ansible.builtin.file"] for task in tasks if task["name"] == name
        )

    for name in ("Create the state directory", "Create the backup directory"):
        arguments = file_task(name)
        assert arguments["owner"] == "65534"
        assert arguments["group"] == "65534"
        assert arguments["mode"] == "0700"

    config = file_task("Create the configuration directory")
    assert config | {"path": None} == {
        "path": None,
        "state": "directory",
        "owner": "root",
        "group": "root",
        "mode": "0700",
    }
    environment = file_task("Enforce the environment file ownership")
    assert environment["owner"] == environment["group"] == "root"
    assert environment["mode"] == "0600"


def test_the_image_is_built_from_the_dockerfile_the_package_publishes():
    """One path, named by `blitzecdn.docker` and pointed at by the role.

    The template used to spell the Dockerfile's location inline, back when
    it sat in a top-level directory: that was a second copy of a location
    the package now owns, and moving the tree
    would have left the controller's `docker compose build` looking for a file
    that is no longer there, and nothing would have said so until an install.

    Relative, because Docker resolves it against the build context rather than
    against anything Ansible knows. That context is the installation directory
    — a source checkout — and it is the last thing on a controller that still
    needs one.
    """
    build = _compose()["services"]["blitzecdn-api"]["build"]
    default = _role_defaults()["blitzecdn_controlplane_dockerfile"]

    assert build["context"] == "/opt/blitzecdn"
    assert build["dockerfile"] == default
    assert not Path(default).is_absolute()
    assert default == str(CONTROL_PLANE_DOCKERFILE.relative_to(REPO_ROOT))
