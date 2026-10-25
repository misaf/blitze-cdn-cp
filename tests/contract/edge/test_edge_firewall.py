"""Firewall rules reaching the rendered configuration."""

from __future__ import annotations

from support.edge_render import (
    _render,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible

REQUIRES_CAPABILITIES = {
    "test_firewall_rules_reach_the_generated_configuration": ("security",),
    "test_the_acme_challenge_path_is_never_filtered": ("geoip", "security"),
    "test_an_allow_country_list_refuses_addresses_the_database_cannot_place": (
        "geoip",
        "security",
    ),
}


def test_firewall_rules_reach_the_generated_configuration(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry.get("firewall")
    )
    rendered = _render(site)
    assert "allow 203.0.113.9/32;" in rendered
    assert "deny 203.0.113.0/24;" in rendered
    assert "deny 2001:db8::/32;" in rendered
    assert 'if ($request_method ~ "^(DELETE|TRACE)$")' in rendered
    assert "location ^~ /admin {" in rendered
    # The allow has to precede the denies: nginx takes the first match, so the
    # reverse order would make every exemption dead.
    assert rendered.index("allow 203.0.113.9/32;") < rendered.index(
        "deny 203.0.113.0/24;"
    )


def test_a_site_without_firewall_rules_renders_exactly_as_before(desired_state):
    """Every existing site must be untouched by the new block."""
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if not entry.get("firewall")
    )
    rendered = _render(site)
    for directive in ("allow ", "deny ", "$blitzecdn_country", "$request_method ~"):
        assert directive not in rendered


def test_the_acme_challenge_path_is_never_filtered():
    """A rule that blocked renewal would surface weeks later, at expiry."""
    site = site_to_ansible(
        site_from_settings(
            {
                "name": "locked",
                "server_names": ["locked.example.com"],
                "origin_host": "origin.example.com",
                "firewall": {
                    "deny_sources": ["0.0.0.0/0", "::/0"],
                    "denied_countries": ["RU"],
                    "denied_methods": ["GET"],
                },
            }
        )
    )
    rendered = _render(site, blitzecdn_edge_geoip_enabled=True)
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    challenge_block = rendered[challenge:block_end]
    for directive in ("deny ", "$blitzecdn_country", "$request_method ~"):
        assert directive not in challenge_block, (
            f"{directive!r} applies to the ACME challenge location, so a site "
            "can filter out its own certificate authority"
        )
    # …while the site itself really is closed.
    assert "deny 0.0.0.0/0;" in rendered
    assert 'if ($blitzecdn_country ~ "^(RU)$")' in rendered


def test_an_allow_country_list_refuses_addresses_the_database_cannot_place():
    """`""` is what geoip2 yields for an unknown address.

    An allow list treats it as "not one of these", which only the negated match
    does; a deny list must not fire on it. The asymmetry is easy to invert.
    """
    site = site_to_ansible(
        site_from_settings(
            {
                "name": "geo",
                "server_names": ["geo.example.com"],
                "origin_host": "origin.example.com",
                "firewall": {"allowed_countries": ["DE", "FR"]},
            }
        )
    )
    rendered = _render(site, blitzecdn_edge_geoip_enabled=True)
    assert 'if ($blitzecdn_country !~ "^(DE|FR)$")' in rendered
