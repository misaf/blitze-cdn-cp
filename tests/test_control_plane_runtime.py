"""Operational contracts for the Docker-owned control-plane runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2
import yaml

from blitzecdn.core import broker

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "ansible/roles/blitzecdn_controlplane"
UNINSTALL = ROOT / "ansible/roles/blitzecdn_uninstall/tasks/main.yml"


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

    dockerfile = (ROOT / "docker/control-plane/Dockerfile").read_text(encoding="utf-8")
    assert "USER nobody:nogroup" in dockerfile
    for duplicate_account in ("useradd", "groupadd", "adduser", "addgroup"):
        assert duplicate_account not in dockerfile
    for dynamic_mapping in ("BLITZE_UID", "BLITZE_GID", "PUID", "PGID"):
        assert dynamic_mapping not in dockerfile
    dockerignore = (ROOT / "docker/control-plane/Dockerfile.dockerignore").read_text(
        encoding="utf-8"
    )
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
        blitzecdn_controlplane_compose_file="/etc/blitzecdn/control-plane.yml"
    )

    assert "docker compose --file" in wrapper
    assert "run --rm" in wrapper
    assert "${#" not in wrapper
    assert "readonly compose_file=/etc/blitzecdn/control-plane.yml" in rendered
    assert 'stop "${running[@]}"' in wrapper
    assert 'up --detach "${running[@]}"' in wrapper
    assert "COMPOSE_RESTORE_OFFLINE=1" in wrapper
    assert "docker exec" not in wrapper


def test_container_ssh_uses_the_mounted_controller_configuration():
    config = (ROOT / "ansible/ansible.cfg").read_text(encoding="utf-8")
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
        ROOT / "ansible/roles/blitzecdn_uninstall/defaults/main.yml",
        ROOT / "ansible/roles/blitzecdn_uninstall/meta/argument_specs.yml",
        ROOT / "ansible/roles/blitzecdn_uninstall/tasks/main.yml",
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
