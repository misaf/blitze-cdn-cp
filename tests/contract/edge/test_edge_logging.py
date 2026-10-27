"""Hostname log isolation across listeners, releases and shared site policy."""

import pytest
import yaml
from support.contracts import _role_defaults
from support.edge_render import ROLE_DIR, _mode_site, _render, _role

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.core.ansible.templating import AnsibleTemplates

REQUIRES_CAPABILITIES = {
    "test_split_hostnames_claim_quic_reuseport_only_once": ("http3",),
}


@pytest.mark.parametrize("mode", [SslMode.OFF, SslMode.FLEXIBLE])
def test_each_hostname_has_its_own_logs_on_every_listener(mode):
    names = ["example.com", "api.example.com", "*.example.com"]
    site = site_to_ansible(_mode_site(mode, mode != SslMode.OFF, server_names=names))
    rendered = _render(site)
    blocks = rendered.split("server {")[1:]
    runtime = _role_defaults()["blitzecdn_edge_runtime"]
    ports = runtime["listeners"]["http"] + (
        runtime["listeners"]["https"] if mode != SslMode.OFF else []
    )
    assert len(blocks) == len(names) * len(ports)
    for hostname in names:
        matching = [b for b in blocks if f"server_name {hostname};" in b]
        assert len(matching) == len(ports)
        path = "/var/log/nginx/domains/" + hostname.replace("*", "_wildcard")
        for block in matching:
            assert f"access_log {path}.access.log blitzecdn;" in block
            assert f"error_log {path}.error.log warn;" in block
            assert block.count("/domains/") == 2
            assert "access_log /var/log/nginx/blitzecdn-access.log blitzecdn;" in block
            assert "access_log /dev/stdout blitzecdn;" in block
            assert "error_log /dev/stderr warn;" in block
            assert block.count("\n    server_name ") == 1


def test_disabling_access_logs_keeps_hostname_error_logs():
    site = site_to_ansible(_mode_site(SslMode.OFF, False))
    rendered = _render(site, blitzecdn_nginx_access_log_enabled=False)
    for block in rendered.split("server {")[1:]:
        assert "access_log off;" in block
        assert ".access.log" not in block
        assert "access_log /dev/stdout" not in block
        assert (
            "error_log /var/log/nginx/domains/mode.example.com.error.log warn;" in block
        )


def test_split_hostnames_claim_quic_reuseport_only_once():
    site = site_to_ansible(
        _mode_site(
            SslMode.FLEXIBLE,
            True,
            server_names=["example.com", "api.example.com"],
            http3_enabled=True,
        )
    )
    rendered = _render(
        site,
        blitzecdn_nginx_default_server=False,
        blitzecdn_nginx_http3_listener_owner=site["name"],
    )
    assert rendered.count("quic reuseport;") == 2  # IPv4 and IPv6, first host only.
    assert rendered.count("quic;") == 2


def test_rotation_uses_the_persistent_log_root_and_reopens_nginx():
    context = _role_defaults()
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
        "create 0640 101 adm",
        "sharedscripts",
    ):
        assert f"    {directive}\n" in rendered
    assert "copytruncate" not in rendered
    assert "--format '{{.State.Running}}'" in rendered
    assert 'kill -USR1 "$(cat /var/run/nginx.pid)"' in rendered


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
    context = _role_defaults()
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
    role = _role("blitzecdn_edge_teardown")
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text())
    assert defaults["blitzecdn_edge_teardown_remove_logs"] is False
    removal = next(t for t in tasks if t["name"] == "Remove hostname logs and archives")
    assert removal["when"] == "blitzecdn_edge_teardown_remove_logs | bool"
    assert removal["ansible.builtin.file"]["state"] == "absent"
