"""The trusted ``BZ-*`` visitor headers on the origin leg."""

from __future__ import annotations

from typing import Any

from support.edge_render import (
    render_site,
    runtime_contract,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.serving.contract import SiteVisitorHeaders

REQUIRES_CAPABILITIES = {
    "test_ip_country_reads_the_variable_the_capability_defines": ("geoip",),
}


def _with_visitor_headers(**headers: bool) -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": "visitor",
                "server_names": ["visitor.example.com"],
                "origin_host": "origin.example.com",
                "visitor_headers": headers,
            }
        )
    )


def test_every_site_sends_each_visitor_header_switch(desired_state):
    """Nested, so invisible to the top-level key sweep."""
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site["visitor_headers"]) == set(SiteVisitorHeaders.model_fields)


def test_the_default_site_sends_the_address_and_not_the_country(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry["name"] == "example-com"
    )
    rendered = render_site(site)

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    assert "$blitzecdn_country" not in rendered


def test_connecting_ip_enabled_sends_the_nginx_connection_address():
    """`$remote_addr`, and nothing a client can write.

    nginx fills it from the socket, identically for IPv4 and IPv6.
    """
    rendered = render_site(_with_visitor_headers(connecting_ip=True))

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    for forgeable in (
        "$http_x_forwarded_for",
        "$http_x_real_ip",
        "$http_bz_connecting_ip",
        "$http_true_client_ip",
        "$http_cf_connecting_ip",
    ):
        assert forgeable not in rendered


def test_connecting_ip_disabled_clears_the_header_rather_than_forwarding_it():
    """Off must not mean "pass the visitor's own version through".

    nginx forwards headers it was not told about; an empty value drops the
    client's header from the upstream request.
    """
    rendered = render_site(_with_visitor_headers(connecting_ip=False))

    assert 'proxy_set_header BZ-Connecting-IP "";' in rendered
    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" not in rendered


def test_ip_country_reads_the_variable_the_capability_defines():
    """The same $blitzecdn_country the firewall rules test, not a second lookup.

    Core's half only; the `geoip2` block defining it is the GeoIP capability's,
    tested there.
    """
    rendered = render_site(_with_visitor_headers(ip_country=True))

    assert "set $blitzecdn_visitor_ip_country $blitzecdn_country;" in rendered
    assert "proxy_set_header BZ-IPCountry $blitzecdn_visitor_ip_country;" in rendered
    assert rendered.count("geoip2") == 0


def test_ip_country_disabled_clears_the_header_rather_than_forwarding_it():
    rendered = render_site(_with_visitor_headers(ip_country=False))

    assert 'set $blitzecdn_visitor_ip_country "";' in rendered
    assert "$blitzecdn_visitor_ip_country $blitzecdn_country" not in rendered
    assert "$blitzecdn_country" not in rendered


def test_a_spoofed_bz_header_is_replaced_in_every_state():
    """Whatever the switches say, the BZ- namespace is written by the edge.

    Both directives are emitted unconditionally, one with a value and one
    clearing the name.
    """
    for headers in (
        {"connecting_ip": True, "ip_country": True},
        {"connecting_ip": True, "ip_country": False},
        {"connecting_ip": False, "ip_country": True},
        {"connecting_ip": False, "ip_country": False},
    ):
        rendered = render_site(
            _with_visitor_headers(**headers), blitzecdn_edge_geoip_enabled=True
        )
        for header in ("BZ-Connecting-IP", "BZ-IPCountry"):
            # Once per server block: the HTTP listeners, and no TLS here.
            emitted = rendered.count(f"proxy_set_header {header} ")
            assert emitted == len(runtime_contract("listeners", "http")), (
                f"{header} is not written on every listener for {headers}"
            )


def test_visitor_headers_never_reach_the_cache_key():
    """Two visitors from different countries share one cached object.

    Keying on either header would multiply every entry by the client count.
    """
    rendered = render_site(
        _with_visitor_headers(connecting_ip=True, ip_country=True),
        blitzecdn_edge_geoip_enabled=True,
    )

    for line in rendered.splitlines():
        if "proxy_cache_key" in line:
            assert "$remote_addr" not in line
            assert "$blitzecdn_country" not in line
            assert "BZ-" not in line


def test_visitor_headers_do_not_disturb_the_existing_forwarding_headers():
    """X-Real-IP and X-Forwarded-For keep their pre-capability behaviour."""
    rendered = render_site(_with_visitor_headers())

    assert "proxy_set_header X-Real-IP $remote_addr;" in rendered
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in rendered
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in rendered


def test_the_acme_challenge_location_carries_no_visitor_headers():
    """It is served from disk; there is no origin request to annotate."""
    rendered = render_site(
        _with_visitor_headers(connecting_ip=True, ip_country=True),
        blitzecdn_edge_geoip_enabled=True,
    )
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))

    assert "BZ-" not in rendered[challenge:block_end]


def test_missing_visitor_headers_preserve_pre_upgrade_behavior():
    """With the block absent, the role's defaults send the address and not the
    country, matching the domain's.
    """
    site = _with_visitor_headers()
    del site["visitor_headers"]

    rendered = render_site(site)

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    assert 'set $blitzecdn_visitor_ip_country "";' in rendered
