"""Edge-wide visitor limits: client timeouts, header buffers and per-address caps.

Before these were rendered, ordinary traffic ran on Nginx's 60s client
timeouts with no per-address connection ceiling, so a slowloris or slow-POST
client held a worker slot for a minute per trickled byte. These tests hold the
rendered directives to the role's settings and the role's validation to
refusing anything that could smuggle a second directive into nginx.conf.
"""

from __future__ import annotations

import re

import pytest
from support.contracts import ROLE_DIR, role_defaults, run_role_tasks
from support.edge_render import nginx_environment


def _render(**overrides: object) -> str:
    return (
        nginx_environment()
        .get_template("nginx.conf.j2")
        .render(blitzecdn_release_digest="test", **(role_defaults() | overrides))
    )


def _http_block(rendered: str) -> str:
    match = re.search(r"^http \{\n(.*)^\}", rendered, re.M | re.S)
    assert match, "nginx.conf renders no http block"
    return match.group(1)


def test_defaults_close_slow_clients_and_cap_one_address():
    http = _http_block(_render())

    for directive in (
        "client_header_timeout 10s;",
        "client_body_timeout 10s;",
        "send_timeout 10s;",
        "keepalive_timeout 65s;",
        "keepalive_requests 1000;",
        "large_client_header_buffers 4 8k;",
        "limit_conn_zone $binary_remote_addr zone=blitzecdn_conn_per_ip:20m;",
        "limit_conn blitzecdn_conn_per_ip 256;",
        "limit_conn_status 429;",
    ):
        assert f"    {directive}\n" in http, directive


def test_limits_are_set_from_controller_settings():
    http = _http_block(
        _render(
            blitzecdn_nginx_client_header_timeout="5s",
            blitzecdn_nginx_client_body_timeout="30s",
            blitzecdn_nginx_send_timeout="20s",
            blitzecdn_nginx_keepalive_requests=500,
            blitzecdn_nginx_large_client_header_buffers="8 16k",
            blitzecdn_nginx_limit_conn_per_ip=1024,
            blitzecdn_nginx_limit_conn_zone_size="64m",
        )
    )

    assert "    client_header_timeout 5s;\n" in http
    assert "    client_body_timeout 30s;\n" in http
    assert "    send_timeout 20s;\n" in http
    assert "    keepalive_requests 500;\n" in http
    assert "    large_client_header_buffers 8 16k;\n" in http
    assert "zone=blitzecdn_conn_per_ip:64m;" in http
    assert "    limit_conn blitzecdn_conn_per_ip 1024;\n" in http


def test_zero_turns_the_connection_limit_off():
    http = _http_block(_render(blitzecdn_nginx_limit_conn_per_ip=0))

    assert "limit_conn" not in http
    # The timeouts are independent of the cap and stay.
    assert "    client_body_timeout 10s;\n" in http


def test_the_limit_applies_to_every_server_through_the_http_context():
    """Declared once at http level, so no site, default server or contributed
    server block can be rendered without it."""
    rendered = _render()
    http = _http_block(rendered)

    assert http.index("limit_conn blitzecdn_conn_per_ip") < http.index("conf.d/*.conf")
    assert http.index("client_body_timeout") < http.index("sites-enabled/*")
    assert rendered.count("limit_conn blitzecdn_conn_per_ip") == 1


def _limits(**overrides: object) -> dict[str, object]:
    defaults = role_defaults()
    return {
        name: value
        for name, value in (defaults | overrides).items()
        if name.startswith("blitzecdn_nginx_")
        and any(
            part in name
            for part in (
                "timeout",
                "keepalive_requests",
                "header_buffers",
                "limit_conn",
            )
        )
    }


def test_the_shipped_defaults_pass_validation(tmp_path):
    result = run_role_tasks(ROLE_DIR / "tasks/visitor-limits.yml", _limits(), tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "override",
    [
        {"blitzecdn_nginx_client_header_timeout": "10s; return 200"},
        {"blitzecdn_nginx_client_body_timeout": "ten seconds"},
        {"blitzecdn_nginx_send_timeout": ""},
        {"blitzecdn_nginx_keepalive_requests": 0},
        {"blitzecdn_nginx_large_client_header_buffers": "4"},
        {"blitzecdn_nginx_large_client_header_buffers": "4 8k; deny all"},
        {"blitzecdn_nginx_limit_conn_per_ip": -1},
        {"blitzecdn_nginx_limit_conn_zone_size": "0"},
    ],
    ids=lambda override: next(iter(override)).removeprefix("blitzecdn_nginx_"),
)
def test_malformed_limits_are_refused_before_rendering_reaches_the_edge(
    tmp_path, override
):
    result = run_role_tasks(
        ROLE_DIR / "tasks/visitor-limits.yml", _limits(**override), tmp_path
    )

    assert result.returncode != 0
    assert "Visitor timeouts must be Nginx times" in result.stdout
