"""Origin connection pooling: the upstream groups a site renders and names.

Before these groups existed every proxy_pass was a literal or variable address,
which Nginx has no pool for, so every cache miss opened a new connection to the
origin — and for an HTTPS origin, a new TLS handshake. These tests hold the
three things pooling depends on: each proxy_pass names a group that is rendered,
each group keeps idle connections, and the request to the origin is one Nginx
is allowed to reuse a connection for.
"""

from __future__ import annotations

import re

from support.contracts import role_defaults
from support.edge_render import (
    mode_site,
    nginx_environment,
    render_site,
    upstream_groups,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.sites.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.tls.contract import SslMode


def _named_groups(rendered: str) -> list[str]:
    return re.findall(r"proxy_pass https?://(\S+);", rendered)


def test_every_proxy_pass_names_a_pooled_group():
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL, serves_tls=True)))
    groups = upstream_groups(rendered)
    defaults = role_defaults()

    named = _named_groups(rendered)
    assert named, "the site proxies nothing"
    assert set(named) == set(groups), "a group is rendered that nothing proxies to"
    for body in groups.values():
        assert (
            f"    keepalive {defaults['blitzecdn_nginx_upstream_keepalive']};" in body
        )
        assert (
            "    keepalive_timeout "
            f"{defaults['blitzecdn_nginx_upstream_keepalive_timeout']};" in body
        )


def test_a_shared_origin_connection_is_declared_once():
    """Flexible's 443 reaches http:80, as the plain 80 listener does.

    Nginx refuses a configuration that declares one upstream name twice, so the
    two listeners must share one group rather than each rendering their own.
    """
    rendered = render_site(
        site_to_ansible(mode_site(SslMode.FLEXIBLE, serves_tls=True))
    )
    declared = re.findall(r"^upstream (\S+) \{", rendered, re.M)

    assert len(declared) == len(set(declared))
    assert "blitzecdn_origin_mode_http_80" in declared
    assert "blitzecdn_origin_mode_http_443" not in declared


def test_groups_are_named_by_site_so_two_sites_never_share_one():
    first = render_site(site_to_ansible(mode_site(SslMode.OFF, serves_tls=False)))
    second = render_site(
        site_to_ansible(
            mode_site(
                SslMode.OFF,
                serves_tls=False,
                name="other",
                server_names=["other.example.com"],
            )
        )
    )

    assert not set(upstream_groups(first)) & set(upstream_groups(second))


def test_with_resolvers_the_origin_follows_dns_from_one_shared_zone():
    """`resolve` keeps what the variable proxy_pass gave: no address pinned at load."""
    defaults = role_defaults()
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL, serves_tls=True)))
    zone = (
        f"    zone {defaults['blitzecdn_nginx_upstream_zone']} "
        f"{defaults['blitzecdn_nginx_upstream_zone_size']};"
    )

    for body in upstream_groups(rendered).values():
        assert zone in body
        assert re.search(r"^    server origin\.example\.com:\d+ resolve;$", body, re.M)


def test_without_resolvers_no_group_asks_for_runtime_resolution():
    rendered = render_site(
        site_to_ansible(mode_site(SslMode.FULL, serves_tls=True)),
        blitzecdn_nginx_resolvers=[],
    )

    for body in upstream_groups(rendered).values():
        assert "resolve" not in body
        assert "zone " not in body


def test_an_ipv6_origin_is_bracketed_in_its_group():
    site = mode_site(SslMode.OFF, serves_tls=False, origin_host="2001:db8::1")
    rendered = render_site(site_to_ansible(site))

    for body in upstream_groups(rendered).values():
        assert re.search(r"^    server \[2001:db8::1\]:\d+ resolve;$", body, re.M)


def test_pooling_can_be_turned_off():
    rendered = render_site(
        site_to_ansible(mode_site(SslMode.FULL, serves_tls=True)),
        blitzecdn_nginx_upstream_keepalive=0,
    )

    assert upstream_groups(rendered)
    assert "keepalive" not in rendered


def test_a_redirecting_listener_contributes_no_group():
    """Always Use HTTPS leaves the HTTP listeners nothing to proxy to."""
    site = mode_site(SslMode.FULL, serves_tls=True, always_use_https=True)
    rendered = render_site(site_to_ansible(site))

    assert all("_https_" in name for name in upstream_groups(rendered))


def test_a_site_that_serves_nothing_renders_no_group():
    """Without resolvers a group resolves at load; an idle site must not fail it."""
    site = site_from_settings(
        {"name": "idle", "server_names": [], "origin_host": "origin.example.com"}
    )
    rendered = render_site(site_to_ansible(site), blitzecdn_nginx_resolvers=[])

    assert upstream_groups(rendered) == {}


def test_the_origin_request_is_one_a_pooled_connection_can_carry():
    """HTTP/1.1, and an empty Connection header unless the visitor upgrades.

    Without both, Nginx closes the origin connection after one response and the
    groups above pool nothing.
    """
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL, serves_tls=True)))
    assert rendered.count("proxy_http_version 1.1;") == len(_named_groups(rendered))
    assert "proxy_set_header Connection $blitzecdn_connection_upgrade;" in rendered

    http = nginx_environment().get_template("http.conf.j2").render(**role_defaults())
    upgrade_map = http.split("map $http_upgrade $blitzecdn_connection_upgrade {", 1)[1]
    assert re.search(r"^\s*''\s+'';$", upgrade_map.split("}", 1)[0], re.M)


def test_responses_leave_in_full_packets():
    rendered = (
        nginx_environment()
        .get_template("nginx.conf.j2")
        .render(blitzecdn_release_digest="test", **role_defaults())
    )

    assert "tcp_nopush on;" in rendered
    assert "tcp_nodelay on;" in rendered
