"""Cross-role wiring: the firewall registry, and the daemon's own configuration."""

import yaml
from contract_support import (
    DOCKER_ROLE_DIR,
    STACK_ROLE_DIR,
    _role,
    ansible_bool,
    jinja2,
)
from paths import CORE_ANSIBLE


def test_http3_uses_the_firewall_registry_for_udp_443():
    role = _role("blitzecdn_firewall")
    tasks = (role / "tasks/main.yml").read_text(encoding="utf-8")
    play = (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")

    assert "['udp|443|any'] if blitzecdn_edge_runtime.listeners.http3 else []" in tasks
    assert "proto: udp" in tasks
    assert "when: blitzecdn_edge_runtime.listeners.http3 | bool" in tasks
    assert "^(tcp|udp)\\|" in tasks
    assert "difference(blitzecdn_firewall_desired_rules)" in tasks
    assert play.index("tasks_from: verify-runtime.yml") < play.index(
        "role: blitzecdn_firewall"
    )
    # And it opens exactly the ports the Nginx role binds, because both read
    # one list. There is no second copy left to keep in step.
    assert "blitzecdn_firewall_http_ports" not in tasks
    assert "blitzecdn_firewall_http_ports" not in (
        role / "defaults/main.yml"
    ).read_text(encoding="utf-8")


def test_the_udp_443_listener_is_verified_where_the_firewall_opened_it():
    """The firewall and the QUIC listener must not be able to disagree.

    The play refuses to converge when the two *desired* states differ. That is
    only half of it: a QUIC bind that fails inside the container leaves UDP/443
    open in the firewall, Alt-Svc advertised to every visitor, and nothing
    listening — an edge that tells browsers to use a protocol it does not
    serve. The health check is the other half.
    """
    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    assert "ss" in health and "-lnu" in health
    assert "blitzecdn_edge_runtime.listeners.http3" in health
    assert "search(':443" in health


def test_docker_daemon_configuration_uses_only_supported_directives():
    """A JSON pseudo-comment is a real key that makes dockerd refuse to start."""
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    environment.filters["bool"] = ansible_bool
    rendered = environment.from_string(
        (DOCKER_ROLE_DIR / "templates/daemon.json.j2").read_text(encoding="utf-8")
    ).render(
        blitzecdn_docker_log_driver="json-file",
        blitzecdn_docker_log_max_size="32m",
        blitzecdn_docker_log_max_file=3,
        blitzecdn_docker_live_restore=True,
    )
    configuration = yaml.safe_load(rendered)

    assert set(configuration) == {"log-driver", "log-opts", "live-restore"}

    tasks = yaml.safe_load(
        (DOCKER_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    )
    configure = next(
        task
        for task in tasks[0]["block"]
        if task["name"] == "Configure the Docker daemon"
    )
    assert configure["ansible.builtin.template"]["validate"] == (
        "/usr/bin/dockerd --validate --config-file %s"
    )


def test_only_the_plays_preparation_carries_the_stage_tag():
    """Staging must never be able to change what an edge is serving.

    `--tags stage` is what a rollout's STAGE phase runs: install the container
    engine, create the persistent directories, pull and digest-pin the runtime
    image, and prove that image's Nginx can serve. Every one of those must be
    true before a configuration is rendered, and not one of them touches the
    configuration — which is what lets a fleet be staged ahead of a window and
    activated inside it.

    A role task that acquired the tag would quietly make staging an activation,
    and the phase would go on reporting itself as the harmless one. The rule is
    positional and checkable: the tag belongs to the edge play's pre-tasks and
    to nothing else in the tree.
    """
    play = yaml.safe_load(
        (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    )[0]

    assert all("stage" in task.get("tags", []) for task in play["pre_tasks"]), (
        "every pre-task is preparation and must run when staging"
    )
    assert not [role for role in play["roles"] if "stage" in role.get("tags", [])], (
        "a role that ran during staging would make the phase a lie"
    )

    offenders = [
        f"{path.relative_to(CORE_ANSIBLE)}: {task.get('name', '<unnamed>')}"
        for path in sorted((CORE_ANSIBLE / "roles").rglob("tasks/*.yml"))
        for task in (yaml.safe_load(path.read_text(encoding="utf-8")) or [])
        if isinstance(task, dict) and "stage" in (task.get("tags") or [])
    ]
    assert offenders == []
