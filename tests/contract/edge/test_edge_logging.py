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
            # The hostname file is the only request log; Docker gets errors only.
            assert block.count("\n    access_log ") == 1
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


def _http_conf(**overrides: object) -> str:
    context = role_defaults()
    context.update(overrides)
    return AnsibleTemplates(context).render(ROLE_DIR / "templates/http.conf.j2")


def _record_format(rendered: str) -> str:
    declaration = rendered.split("log_format blitzecdn_json escape=json", 1)[1]
    return "".join(re.findall(r"'([^']*)'", declaration.split(";", 1)[0]))


def _map(rendered: str, source: str, target: str) -> list[tuple[str, str]]:
    """One rendered map's regex entries, as Python patterns and replacements."""
    body = rendered.split(f"map {source} {target} {{", 1)[1].split("}\n", 1)[0]
    entries = []
    for line in body.strip().splitlines():
        key, value = line.strip().rstrip(";").rsplit(None, 1)
        key = key.strip('"')
        if key == "default":
            continue
        flags = "(?i)" if key.startswith("~*") else ""
        pattern = flags + key.lstrip("~*").replace("(?<", "(?P<")
        entries.append((pattern, value))
    return entries


def _apply_map(entries: list[tuple[str, str]], value: str) -> str:
    for pattern, replacement in entries:
        if match := re.search(pattern, value):
            return re.sub(r"\$(\w+)", lambda m: match.group(m.group(1)), replacement)
    return ""


@pytest.mark.parametrize("bytes_sent", [0, 1234, 2**53 + 1])
def test_hostname_json_records_have_numeric_counters_and_actual_hostnames(bytes_sent):
    rendered = _http_conf(
        blitzecdn_nginx_log_query_strings=True, blitzecdn_nginx_log_client_ip="full"
    )
    record = _record_format(rendered)
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
        "upstream_cache_status": "",
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
    assert result["client_ip"] == variables["remote_addr"]


def test_records_drop_query_strings_and_truncate_addresses_by_default():
    defaults = role_defaults()
    assert defaults["blitzecdn_nginx_log_query_strings"] is False
    assert defaults["blitzecdn_nginx_log_client_ip"] == "truncated"
    rendered = _http_conf()
    record = _record_format(rendered)
    for raw in ('$request"', "$remote_addr", "$http_referer"):
        assert raw not in record
    assert '"request":"$request_method $blitzecdn_log_path $server_protocol"' in record
    assert '"client_ip":"$blitzecdn_log_client_ip"' in record
    assert '"referer":"$blitzecdn_log_referer"' in record

    path = _map(rendered, "$request_uri", "$blitzecdn_log_path")
    assert _apply_map(path, "/reset?token=secret") == "/reset"
    assert _apply_map(path, "/a%3Fb?x=1?y=2") == "/a%3Fb"
    assert _apply_map(path, "/plain") == "/plain"
    referer = _map(rendered, "$http_referer", "$blitzecdn_log_referer")
    assert _apply_map(referer, "https://a.test/p?sig=1#f") == "https://a.test/p"
    assert _apply_map(referer, "") == ""


@pytest.mark.parametrize(
    "address,logged",
    [
        ("192.0.2.123", "192.0.2.0"),
        ("2001:db8:85a3:8d3:1319:8a2e:370:7348", "2001:db8:85a3::"),
        ("2001:DB8:1::7", "2001:DB8:1::"),
        ("2001:db8::1", "2001:db8::"),
        # `::` after the first hextet may hide a non-zero third one: keep less.
        ("2001::2:3:4:5:6:7", "2001::"),
        ("::1", "::"),
        ("::ffff:192.0.2.1", "::"),
        ("unix:", ""),
    ],
)
def test_truncated_addresses_keep_at_most_a_24_or_48(address, logged):
    entries = _map(_http_conf(), "$remote_addr", "$blitzecdn_log_client_ip")
    assert _apply_map(entries, address) == logged


def test_full_logging_keeps_raw_fields_and_renders_no_redaction_maps():
    rendered = _http_conf(
        blitzecdn_nginx_log_query_strings=True, blitzecdn_nginx_log_client_ip="full"
    )
    record = _record_format(rendered)
    assert '"request":"$request"' in record
    assert '"client_ip":"$remote_addr"' in record
    assert '"referer":"$http_referer"' in record
    assert "$blitzecdn_log_" not in rendered


def test_log_formats_follow_the_configured_name_and_access_logging_switch():
    context = role_defaults()
    context["blitzecdn_nginx_log_format_name"] = "custom"
    templates = AnsibleTemplates(context)
    rendered = templates.render(ROLE_DIR / "templates/http.conf.j2")
    assert rendered.count("log_format ") == 1
    assert "log_format custom escape=json" in rendered
    site = render_site(
        site_to_ansible(mode_site(SslMode.OFF, False)),
        blitzecdn_nginx_log_format_name="custom",
    )
    assert ".access.log custom;" in site
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
    # The global error log shares the stanza: nothing in the mount grows unbounded.
    assert "\n/srv/edge-logs/error.log\n/srv/edge-logs/domains/*.log {" in rendered
    for directive in (
        "daily",
        "maxsize 100M",
        "rotate 14",
        "maxage 14",
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


def test_rotation_keeps_logs_for_the_configured_retention():
    context = role_defaults()
    context["blitzecdn_nginx_log_retention_days"] = 3
    rendered = AnsibleTemplates(context).render(ROLE_DIR / "templates/logrotate.j2")
    assert "    rotate 3\n" in rendered
    assert "    maxage 3\n" in rendered


@pytest.mark.parametrize("days,valid", [(0, False), (1, True), (14, True)])
def test_retention_below_one_day_is_refused(days, valid):
    tasks = yaml.safe_load((ROLE_DIR / "tasks/validate.yml").read_text())
    check = next(t for t in tasks if t["name"].startswith("Validate log retention"))
    context = AnsibleTemplates({"blitzecdn_nginx_log_retention_days": days})
    results = [
        context.expression("{{ " + condition + " }}")
        for condition in check["ansible.builtin.assert"]["that"]
    ]
    assert all(results) is valid


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
    error_logs = next(
        t for t in tasks if t["name"] == "Remove the global error log and its archives"
    )
    assert error_logs["when"] == "blitzecdn_edge_teardown_remove_logs | bool"


def test_teardown_log_paths_follow_the_runtime_log_root():
    defaults = yaml.safe_load(
        (role_path("blitzecdn_edge_teardown") / "defaults/main.yml").read_text()
    )
    logs = role_defaults()["blitzecdn_edge_runtime"]["paths"]["logs"]
    assert defaults["blitzecdn_edge_teardown_domain_logs_path"] == logs + "/domains"
    assert defaults["blitzecdn_edge_teardown_error_log_path"] == logs + "/error.log"
