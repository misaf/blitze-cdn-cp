"""Operational contracts for the Docker-owned control-plane runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2
import yaml

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
        blitzecdn_controlplane_image="blitzecdn-control-plane:test",
        blitzecdn_controlplane_install_dir="/opt/blitzecdn",
        blitzecdn_controlplane_service_uid=991,
        blitzecdn_controlplane_service_gid=991,
        blitzecdn_controlplane_config_dir="/etc/blitzecdn",
        blitzecdn_controlplane_service_home="/var/lib/blitzecdn",
        blitzecdn_controlplane_backup_dir="/var/backups/blitzecdn",
        blitzecdn_controlplane_redis_image="redis:test",
    )
    return yaml.safe_load(rendered)


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


def test_cli_is_ephemeral_and_not_a_persistent_daemon():
    cli = _compose()["services"]["blitzecdn-cli"]
    assert cli["profiles"] == ["cli"]
    assert cli["restart"] == "no"
    assert cli["entrypoint"] == ["blitzecdn"]


def test_host_wrapper_uses_compose_for_commands_and_offline_restore():
    wrapper = (ROLE / "templates/blitzecdn-cli.j2").read_text(encoding="utf-8")
    assert "docker compose --file" in wrapper
    assert "run --rm" in wrapper
    assert 'stop "${running[@]}"' in wrapper
    assert 'up --detach "${running[@]}"' in wrapper
    assert "COMPOSE_RESTORE_OFFLINE=1" in wrapper
    assert "docker exec" not in wrapper


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
