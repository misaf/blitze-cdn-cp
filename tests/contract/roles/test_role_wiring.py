"""Cross-role wiring: the firewall registry, and the daemon's own configuration."""

import json

import yaml
from support.contracts import (
    DOCKER_ROLE_DIR,
    STACK_ROLE_DIR,
    role_path,
)
from support.paths import CORE_ANSIBLE


def test_http3_uses_the_firewall_registry_for_udp_443():
    role = role_path("blitzecdn_firewall")
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

    The play refuses differing desired states, but a QUIC bind failing in the
    container would leave UDP/443 open and Alt-Svc advertised with nothing
    listening. The health check covers that.
    """
    health = (STACK_ROLE_DIR / "tasks/health.yml").read_text(encoding="utf-8")
    assert "ss" in health and "-lnu" in health
    assert "blitzecdn_edge_runtime.listeners.http3" in health
    assert "search(':443" in health


def test_docker_daemon_configuration_uses_only_supported_directives():
    """A JSON pseudo-comment is a real key that makes dockerd refuse to start."""
    configuration = json.loads(
        (DOCKER_ROLE_DIR / "files/daemon.json").read_text(encoding="utf-8")
    )

    assert set(configuration) == {"log-driver", "log-opts", "live-restore"}

    tasks = yaml.safe_load(
        (DOCKER_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    )
    configure = next(
        task
        for task in tasks[0]["block"]
        if task["name"] == "Configure the Docker daemon"
    )
    assert configure["ansible.builtin.copy"]["src"] == "daemon.json"
    assert configure["ansible.builtin.copy"]["validate"] == (
        "/usr/bin/dockerd --validate --config-file %s"
    )


def test_staging_writes_a_candidate_and_cannot_start_the_runtime():
    play = yaml.safe_load(
        (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    )[0]
    staged = [role["role"] for role in play["roles"] if "stage" in role.get("tags", [])]
    assert staged == ["blitzecdn_capabilities", "blitzecdn_nginx"]
    assert not [
        role for role in play["roles"] if role["role"] == "blitzecdn_edge_stack"
    ]
    transaction = yaml.safe_load(
        (role_path("blitzecdn_config_release") / "tasks/main.yml").read_text()
    )
    assert [
        (task["ansible.builtin.import_tasks"], task["tags"]) for task in transaction
    ] == [
        ("stage.yml", ["stage"]),
        ("seal.yml", ["stage"]),
        ("activate.yml", ["activate"]),
        ("rollback.yml", ["never", "rollback"]),
        ("commit.yml", ["never", "commit"]),
    ]
