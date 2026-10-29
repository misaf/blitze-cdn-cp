"""Operational contracts for the Docker-owned control-plane runtime."""

from __future__ import annotations

import json
import shlex
from typing import Any

import jinja2
import yaml
from support.paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.assets.docker import (
    CONTROL_PLANE_DOCKERFILE,
    CONTROL_PLANE_DOCKERIGNORE,
)

ROOT = REPO_ROOT
ROLE = CORE_ANSIBLE / "roles/blitzecdn_controlplane"
UNINSTALL = CORE_ANSIBLE / "roles/blitzecdn_uninstall/tasks/main.yml"


def _role_defaults() -> dict[str, Any]:
    """The role's own `defaults/main.yml`.

    Read rather than restated, so every variable the template needs is proved to
    have a default that renders.
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
    )
    return yaml.safe_load(rendered)


def test_control_plane_uses_a_project_name_distinct_from_the_edge_stack():
    assert _compose()["name"] == "blitzecdn-control-plane"


def test_the_api_and_the_worker_are_the_only_persistent_services():
    """The API and the worker are the only persistent services.

    Durable work is rows in the control plane's database, so the two share the
    state volume rather than a queue server.
    """
    services = _compose()["services"]
    assert set(services) == {
        "blitzecdn-api",
        "blitzecdn-worker",
        "blitzecdn-cli",
    }
    assert services["blitzecdn-api"]["command"] == [
        "python",
        "-m",
        "blitzecdn.entrypoints.api",
    ]
    assert services["blitzecdn-worker"]["command"] == [
        "python",
        "-m",
        "blitzecdn.entrypoints.worker",
    ]
    assert services["blitzecdn-api"]["restart"] == "unless-stopped"
    assert services["blitzecdn-worker"]["restart"] == "unless-stopped"
    assert "healthcheck" in services["blitzecdn-api"]
    assert "healthcheck" in services["blitzecdn-worker"]
    for service in services.values():
        assert "depends_on" not in service


def test_no_volume_is_declared_for_a_broker_that_is_not_there():
    """A stale named volume outlives the service that used it."""
    assert "volumes" not in _compose()


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
        "/etc/blitzecdn/blitzecdn.toml:/opt/blitzecdn/blitzecdn.toml:ro",
    }
    for name in ("blitzecdn-api", "blitzecdn-worker"):
        assert set(services[name]["volumes"]) == shared
        assert all("docker.sock" not in mount for mount in services[name]["volumes"])
    assert set(services["blitzecdn-cli"]["volumes"]) == shared | {
        "/var/backups/blitzecdn:/var/backups/blitzecdn"
    }


def _wrapper() -> str:
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701
    environment.filters["quote"] = shlex.quote
    return environment.from_string(
        (ROLE / "templates/blitzecdn-cli.j2").read_text(encoding="utf-8")
    ).render(**_role_defaults())


def test_host_wrapper_runs_the_sdk_runner_in_the_installed_image():
    """The host needs Docker and nothing else, the CLI included."""
    rendered = _wrapper()
    image = _role_defaults()["blitzecdn_controlplane_image"]
    assert f"--entrypoint python \\\n  {image} \\\n" in rendered
    assert "-m blitzecdn.host.host_runtime" in rendered
    assert '--services /etc/blitzecdn/control-plane.compose.yml -- "$@"' in rendered
    assert "sudo --preserve-env=BLITZE_ALLOW_EMPTY_SITES" in rendered
    assert ".venv" not in rendered
    assert "docker compose" not in rendered
    tasks = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    assert "host_runtime.py" not in tasks


def test_host_wrapper_mounts_every_host_path_the_runner_names_at_that_path():
    """The runner asks the daemon for bind mounts by host path and publishes
    restored files in place, so a path seen differently inside its container
    would mount or overwrite the wrong thing.
    """
    rendered = _wrapper()
    for path in ("/etc/blitzecdn", "/var/lib/blitzecdn", "/run/blitzecdn"):
        assert f"--volume {path}:{path} " in rendered
    assert "--volume /var/run/docker.sock:/var/run/docker.sock " in rendered
    # Restore staging must land where the daemon can see it.
    assert "--env TMPDIR=/run/blitzecdn " in rendered
    assert "install -d -m 0700 /run/blitzecdn" in rendered
    # Root for the socket and the restored files' ownership; nothing else.
    assert "--user 0:0" in rendered
    assert "--network none" in rendered
    # Every file the runner publishes lives under the mounted configuration
    # directory, so an atomic replace stays on one filesystem.
    compose = _compose()["services"]["blitzecdn-cli"]
    sources = [mount.split(":")[0] for mount in compose["volumes"]]
    assert "/etc/blitzecdn/blitzecdn.toml" in sources
    assert compose["env_file"] == ["/etc/blitzecdn/blitzecdn.env"]


def test_host_wrapper_passes_the_empty_sites_override_only_when_set():
    rendered = _wrapper()
    assert "${BLITZE_ALLOW_EMPTY_SITES+set}" in rendered
    assert "environment=(--env BLITZE_ALLOW_EMPTY_SITES)" in rendered


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
        == "Remove the control-plane containers and any volumes they hold"
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


def test_the_services_run_the_image_the_installer_obtained():
    """Compose never builds or pulls: install.sh did, before the play that runs
    inside that image."""
    for service in _compose()["services"].values():
        assert service["image"] == "blitzecdn-control-plane:test"
        assert service["pull_policy"] == "never"
        assert "build" not in service
    tasks = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    for local_build in ("- build", "docker build", "--pull"):
        assert local_build not in tasks


def _backup_unit(name: str, **overrides: Any) -> str:
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701
    return environment.from_string(
        (ROLE / f"templates/{name}.j2").read_text(encoding="utf-8")
    ).render(**(_role_defaults() | overrides))


def test_scheduled_backups_run_through_the_host_wrapper_and_prune_after():
    """The wrapper stages the root-only environment file into the archive; a
    container on its own cannot read it, so the schedule lives on the host."""
    service = _backup_unit("blitzecdn-backup.service")
    starts = [line for line in service.splitlines() if line.startswith("ExecStart=")]
    assert starts == [
        "ExecStart=/usr/local/bin/blitzecdn backup create",
        "ExecStart=/usr/local/bin/blitzecdn backup prune",
    ]
    assert "Type=oneshot" in service
    assert "After=docker.service" in service

    timer = _backup_unit("blitzecdn-backup.timer")
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    disabled = _backup_unit(
        "blitzecdn-backup.timer", blitzecdn_controlplane_backup_schedule=""
    )
    assert "OnCalendar" not in disabled


def test_the_backup_timer_follows_its_schedule_and_the_services():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks]
    install = names.index("Install the scheduled backup units")
    assert names.index("Wait for the API and worker containers to become healthy") < (
        install
    )
    units = tasks[install]
    assert units["loop"] == ["blitzecdn-backup.service", "blitzecdn-backup.timer"]
    assert units["ansible.builtin.template"]["mode"] == "0644"
    timer = tasks[names.index("Schedule the backup timer")]
    assert timer["ansible.builtin.systemd_service"]["name"] == "blitzecdn-backup.timer"
    defaults = _role_defaults()
    assert defaults["blitzecdn_controlplane_backup_schedule"] == "daily"
    assert defaults["blitzecdn_controlplane_backup_retain_count"] == 7
    config = (ROLE / "templates/blitzecdn.toml.j2").read_text(encoding="utf-8")
    assert (
        "backup_retain_count = {{ blitzecdn_controlplane_backup_retain_count | int }}"
        in config
    )


def test_uninstall_stops_the_backup_timer_before_the_project_and_keeps_archives():
    tasks = yaml.safe_load(UNINSTALL.read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks]
    assert names.index("Stop the scheduled backup timer") < names.index(
        "Remove the control-plane containers and any volumes they hold"
    )
    removed = tasks[names.index("Remove the scheduled backup units")]
    assert removed["loop"] == ["blitzecdn-backup.timer", "blitzecdn-backup.service"]
    assert "backup" not in json.dumps(
        tasks[names.index("Remove exact BlitzeCDN-owned host paths")]
    )


def test_edge_teardown_leaves_the_controller_backup_units_alone():
    """A standalone controller is also an edge; removing that edge keeps backups."""
    tasks = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_edge_teardown/tasks/main.yml").read_text(
            encoding="utf-8"
        )
    )
    find = next(
        task["ansible.builtin.find"]
        for task in tasks
        if task.get("name") == "Find edge-owned timers and service units"
    )
    excluded = set(find["excludes"].split(","))
    assert {"blitzecdn-backup.service", "blitzecdn-backup.timer"} <= excluded
