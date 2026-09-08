"""The trusted ``BZ-*`` visitor headers on the origin leg."""

from __future__ import annotations

from typing import Any

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from edge_render_support import (
    _contract,
    _render,
    _role_spec,
)

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
)
from blitzecdn.capabilities.dns.policy import SiteVisitorHeaders

REQUIRES_CAPABILITIES = {
    "test_ip_country_reads_the_variable_the_capability_defines": ("geoip",),
}


def _with_visitor_headers(**headers: bool) -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "visitor",
                "server_names": ["visitor.example.com"],
                "origin_host": "origin.example.com",
                "visitor_headers": headers,
            }
        )
    )


def test_every_emitted_visitor_header_key_is_declared_by_the_role(desired_state):
    """Nested like the firewall, and invisible to the top-level key sweep."""
    declared = set(
        _role_spec()["blitzecdn_nginx_sites"]["options"]["visitor_headers"]["options"]
    )
    assert set(SiteVisitorHeaders.model_fields) == declared, (
        "SiteVisitorHeaders and the role's visitor_headers suboptions disagree: "
        f"{sorted(set(SiteVisitorHeaders.model_fields) ^ declared)}"
    )
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site["visitor_headers"]) == declared


def test_the_default_site_sends_the_address_and_not_the_country(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry["name"] == "example-com"
    )
    rendered = _render(site)

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    assert "$blitzecdn_country" not in rendered


def test_connecting_ip_enabled_sends_the_nginx_connection_address():
    """`$remote_addr`, and nothing a client can write.

    The peer address of the accepted connection is the only value at the edge
    that a request header cannot influence, and it renders identically for IPv4
    and IPv6 — nginx fills the variable from the socket.
    """
    rendered = _render(_with_visitor_headers(connecting_ip=True))

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

    nginx forwards request headers it was not told about, so omitting the
    directive would hand the origin a BZ-Connecting-IP the client wrote. An
    empty value is dropped from the upstream request and takes the client's
    header with it.
    """
    rendered = _render(_with_visitor_headers(connecting_ip=False))

    assert 'proxy_set_header BZ-Connecting-IP "";' in rendered
    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" not in rendered


def test_ip_country_reads_the_variable_the_capability_defines():
    """The same $blitzecdn_country the firewall rules test, not a second lookup.

    Core's half only. The `geoip2` block that *defines* the variable is the
    `blitzecdn-geoip` distribution's, and that both sides spell the name the
    same way is asserted in that package's own tests, which can read both.
    Repeating it here would make this suite fail on a checkout where the
    optional distribution is not installed.
    """
    rendered = _render(_with_visitor_headers(ip_country=True))

    assert "set $blitzecdn_visitor_ip_country $blitzecdn_country;" in rendered
    assert "proxy_set_header BZ-IPCountry $blitzecdn_visitor_ip_country;" in rendered
    assert rendered.count("geoip2") == 0


def test_ip_country_disabled_clears_the_header_rather_than_forwarding_it():
    rendered = _render(_with_visitor_headers(ip_country=False))

    assert 'set $blitzecdn_visitor_ip_country "";' in rendered
    assert "$blitzecdn_visitor_ip_country $blitzecdn_country" not in rendered
    assert "$blitzecdn_country" not in rendered


def test_a_spoofed_bz_header_is_replaced_in_every_state():
    """Whatever the switches say, the BZ- namespace is written by the edge.

    Both directives are emitted unconditionally — one carries a value, the
    other clears the name — so a client-supplied BZ-Connecting-IP or
    BZ-IPCountry can never reach the origin unmodified.
    """
    for headers in (
        {"connecting_ip": True, "ip_country": True},
        {"connecting_ip": True, "ip_country": False},
        {"connecting_ip": False, "ip_country": True},
        {"connecting_ip": False, "ip_country": False},
    ):
        rendered = _render(
            _with_visitor_headers(**headers), blitzecdn_edge_geoip_enabled=True
        )
        for header in ("BZ-Connecting-IP", "BZ-IPCountry"):
            # Once per server block: the HTTP listeners, and no TLS here.
            emitted = rendered.count(f"proxy_set_header {header} ")
            assert emitted == len(_contract("listeners", "http")), (
                f"{header} is not written on every listener for {headers}"
            )


def test_visitor_headers_never_reach_the_cache_key():
    """Two visitors from different countries share one cached object.

    Putting either header in the key would multiply every entry by the number
    of distinct client addresses, which is the whole cache.
    """
    rendered = _render(
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
    rendered = _render(_with_visitor_headers())

    assert "proxy_set_header X-Real-IP $remote_addr;" in rendered
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in rendered
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in rendered


def test_the_acme_challenge_location_carries_no_visitor_headers():
    """It is served from disk; there is no origin request to annotate."""
    rendered = _render(
        _with_visitor_headers(connecting_ip=True, ip_country=True),
        blitzecdn_edge_geoip_enabled=True,
    )
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))

    assert "BZ-" not in rendered[challenge:block_end]


def test_missing_visitor_headers_preserve_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role.

    The role's defaults apply, which means the visitor address is sent and the
    country is not — exactly what both sides do once the controller catches up.
    """
    site = _with_visitor_headers()
    del site["visitor_headers"]

    rendered = _render(site)

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    assert 'set $blitzecdn_visitor_ip_country "";' in rendered
