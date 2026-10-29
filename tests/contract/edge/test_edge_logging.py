"""Hostname log isolation and the request records used for usage collection."""

import json
import re
from string import Template

import pytest
import yaml
from support.contracts import role_defaults
from support.edge_render import ROLE_DIR, mode_site, render_site, role_path

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.core.ansible.templating import AnsibleTemplates

REQUIRES_CAPABILITIES = {
    "test_split_hostnames_claim_quic_reuseport_only_once": ("http3",),
}


@pytest.mark.parametrize("mode", [SslMode.OFF, SslMode.FLEXIBLE])
def test_each_hostname_has_its_own_logs_on_every_listener(mode):
    names = ["example.com", "api.example.com", "*.example.com"]
    site = site_to_ansible(mode_site(mode, mode != SslMode.OFF, server_names=names))
    rendered = render_site(site)
    blocks = rendered.split("server {")[1:]
    runtime = role_defaults()["blitzecdn_edge_runtime"]
    ports = runtime["listeners"]["http"] + (
        runtime["listeners"]["https"] if mode != SslMode.OFF else []
    )
    assert len(blocks) == len(names) * len(ports)
    for hostname in names:
        matching = [b for b in blocks if f"server_name {hostname};" in b]
        assert len(matching) == len(ports)
        path = "/var/log/nginx/domains/" + hostname.replace("*", "_wildcard")
        for block in matching:
            assert f"access_log {path}.access.log blitzecdn_json;" in block
            assert f"error_log {path}.error.log warn;" in block
            assert block.count("/domains/") == 2
            assert "access_log /var/log/nginx/blitzecdn-access.log blitzecdn;" in block
            assert "access_log /dev/stdout blitzecdn;" in block
            assert "error_log /dev/stderr warn;" in block
            assert block.count("\n    server_name ") == 1


def test_disabling_access_logs_keeps_hostname_error_logs():
    site = site_to_ansible(mode_site(SslMode.OFF, False))
    rendered = render_site(site, blitzecdn_nginx_access_log_enabled=False)
    for block in rendered.split("server {")[1:]:
        assert "access_log off;" in block
        assert ".access.log" not in block
        assert "access_log /dev/stdout" not in block
        assert (
            "error_log /var/log/nginx/domains/mode.example.com.error.log warn;" in block
        )


@pytest.mark.parametrize("bytes_sent", [0, 1234, 2**53 + 1])
def test_hostname_json_records_have_numeric_counters_and_actual_hostnames(bytes_sent):
    rendered = AnsibleTemplates(role_defaults()).render(
        ROLE_DIR / "templates/http.conf.j2"
    )
    declaration = rendered.split("log_format blitzecdn_json escape=json", 1)[1]
    record = "".join(re.findall(r"'([^']*)'", declaration))
    # Substitute JSON-escaped variable values into the shipped format to check
    # its JSON structure and types. Native Nginx escaping is checked in Docker.
    variables = {
        "request_id": "a" * 32,
        "time_iso8601": "2026-10-27T12:30:00+00:00",
        "host": "api.example.com",
        "server_name": "*.example.com",
        "bytes_sent": bytes_sent,
        "body_bytes_sent": max(bytes_sent - 100, 0),
        "status": 200,
        "request_time": 0.125,
        "remote_addr": "2001:db8::1",
        "request": 'GET /path?q="quoted" HTTP/2.0',
        "sent_http_x_cache_status": "",
        "upstream_addr": "-",
        "upstream_response_time": "-",
        "http_referer": "",
        "http_user_agent": 'client "quoted" \\ newline\nnext',
    }
    encoded = {
        key: json.dumps(value)[1:-1] if isinstance(value, str) else str(value)
        for key, value in variables.items()
    }
    line = Template(record).substitute(encoded)
    result = json.loads(line)
    assert "\n" not in line
    assert result["schema_version"] == 1
    assert result["hostname"] == "api.example.com"
    assert result["server_name"] == "*.example.com"
    assert result["timestamp"] == variables["time_iso8601"]
    assert result["bytes_sent"] == bytes_sent
    assert type(result["bytes_sent"]) is int
    assert type(result["body_bytes_sent"]) is int
    assert type(result["status"]) is int
    assert result["request_time"] == 0.125
    assert result["upstream_response_time"] == "-"
    assert result["user_agent"] == variables["http_user_agent"]
    assert result["request"] == variables["request"]


def test_log_formats_follow_the_configured_name_and_access_logging_switch():
    context = role_defaults()
    context["blitzecdn_nginx_log_format_name"] = "custom"
    templates = AnsibleTemplates(context)
    rendered = templates.render(ROLE_DIR / "templates/http.conf.j2")
    assert "log_format custom escape=json" in rendered
    assert "log_format custom_json escape=json" in rendered
    site = render_site(
        site_to_ansible(mode_site(SslMode.OFF, False)),
        blitzecdn_nginx_log_format_name="custom",
    )
    assert ".access.log custom_json;" in site
    assert "blitzecdn-access.log custom;" in site
    assert "access_log /dev/stdout custom;" in site
    context["blitzecdn_nginx_access_log_enabled"] = False
    assert "log_format " not in AnsibleTemplates(context).render(
        ROLE_DIR / "templates/http.conf.j2"
    )


def test_split_hostnames_claim_quic_reuseport_only_once():
    site = site_to_ansible(
        mode_site(
            SslMode.FLEXIBLE,
            True,
            server_names=["example.com", "api.example.com"],
            http3_enabled=True,
        )
    )
    rendered = render_site(
        site,
        blitzecdn_nginx_default_server=False,
        blitzecdn_nginx_http3_listener_owner=site["name"],
    )
    assert rendered.count("quic reuseport;") == 2  # IPv4 and IPv6, first host only.
    assert rendered.count("quic;") == 2


def test_rotation_uses_the_persistent_log_root_and_reopens_nginx():
    context = role_defaults()
    context["blitzecdn_edge_runtime"]["paths"]["logs"] = "/srv/edge-logs"
    rendered = AnsibleTemplates(context).render(ROLE_DIR / "templates/logrotate.j2")
    assert "/srv/edge-logs/domains/*.log {" in rendered
    for directive in (
        "daily",
        "maxsize 100M",
        "rotate 14",
        "missingok",
        "notifempty",
        "compress",
        "delaycompress",
        "su root adm",
        "create 0640",
        "sharedscripts",
    ):
        assert f"    {directive}\n" in rendered
    assert "copytruncate" not in rendered
    assert "--format '{{.State.Running}}'" in rendered
    assert 'kill -USR1 "$(cat /var/run/nginx.pid)"' in rendered


@pytest.mark.parametrize(
    "check_mode,install_needed,expected",
    [(True, True, False), (True, False, True), (False, True, True)],
)
def test_rotation_configuration_waits_for_package_installation(
    check_mode, install_needed, expected
):
    tasks = yaml.safe_load((ROLE_DIR / "tasks/logging.yml").read_text())
    context = AnsibleTemplates(
        {
            "ansible_check_mode": check_mode,
            "blitzecdn_nginx_logrotate_apt": {"changed": install_needed},
        }
    )
    for task in tasks:
        if (
            "ansible.builtin.template" in task
            or "ansible.builtin.systemd_service" in task
        ):
            assert context.expression("{{ " + task["when"] + " }}") is expected


def test_log_files_are_private_and_workers_can_reopen_them():
    tasks = yaml.safe_load((ROLE_DIR / "tasks/logging.yml").read_text())
    directory = tasks[0]["ansible.builtin.file"]
    assert directory["owner"] == "root"
    assert directory["mode"] == "0751"
    files = tasks[1]["ansible.builtin.file"]
    assert files["owner"] == "{{ blitzecdn_edge_runtime.worker_uid }}"
    assert files["group"] == "adm"
    assert files["mode"] == "0640"
    assert files["access_time"] == files["modification_time"] == "preserve"
    context = role_defaults()
    context["blitzecdn_nginx_sites"] = [
        {"server_names": ["example.com", "*.example.com"]},
        {"server_names": ["example.com", "api.example.com"]},
    ]
    targets = AnsibleTemplates(context).expression(tasks[1]["loop"])
    assert {tuple(target) for target in targets} == {
        (name, kind)
        for name in ("example.com", "*.example.com", "api.example.com")
        for kind in ("access", "error")
    }
    assert len(targets) == 6


def test_teardown_retains_hostname_logs_unless_log_removal_is_requested():
    role = role_path("blitzecdn_edge_teardown")
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text())
    assert defaults["blitzecdn_edge_teardown_remove_logs"] is False
    removal = next(t for t in tasks if t["name"] == "Remove hostname logs and archives")
    assert removal["when"] == "blitzecdn_edge_teardown_remove_logs | bool"
    assert removal["ansible.builtin.file"]["state"] == "absent"
