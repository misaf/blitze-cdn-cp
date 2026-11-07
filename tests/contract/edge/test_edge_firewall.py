"""Firewall rules reaching the rendered configuration."""

from __future__ import annotations

from support.edge_render import (
    render_site,
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
    "test_allowed_methods_refuse_every_other_method": ("security",),
    "test_allowed_paths_match_the_normalized_uri_with_escaped_entries": ("security",),
    "test_method_and_path_allowlists_combine": ("security",),
    "test_the_acme_challenge_path_is_served_under_a_path_allowlist": ("security",),
}


def _render(firewall):
    return render_site(
        site_to_ansible(
            site_from_settings(
                {
                    "name": "allow",
                    "server_names": ["allow.example.com"],
                    "origin_host": "origin.example.com",
                    "firewall": firewall,
                }
            )
        )
    )


def test_firewall_rules_reach_the_generated_configuration(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry.get("firewall")
    )
    rendered = render_site(site)
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
    rendered = render_site(site)
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
    rendered = render_site(site, blitzecdn_edge_geoip_enabled=True)
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
    rendered = render_site(site, blitzecdn_edge_geoip_enabled=True)
    assert 'if ($blitzecdn_country !~ "^(DE|FR)$")' in rendered


def test_allowed_methods_refuse_every_other_method():
    rendered = _render({"allowed_methods": ["GET"]})
    assert 'if ($request_method !~ "^(GET|HEAD)$") { return 405; }' in rendered
    assert "$uri !~" not in rendered


def test_allowed_paths_match_the_normalized_uri_with_escaped_entries():
    """$uri is decoded and slash-merged, so /%61pi/ and //api/ match like /api/.

    $request_uri is the raw line: matching it would let either spelling past.
    A trailing '/' is a prefix; anything else is exact, and a '.' is a dot.
    """
    rendered = _render({"allowed_paths": ["/api/", "/status", "/v1.0/"]})
    assert 'if ($uri !~ "^(/api/|/status$|/v1\\.0/)") { return 403; }' in rendered
    assert "$request_uri !~" not in rendered
    assert "$request_method !~" not in rendered


def test_method_and_path_allowlists_combine():
    rendered = _render(
        {
            "allowed_methods": ["GET"],
            "allowed_paths": ["/api/"],
            "denied_paths": ["/api/admin"],
        }
    )
    location = rendered[rendered.index("    location / {") :]
    location = location[: location.index("try_files")]
    assert '$request_method !~ "^(GET|HEAD)$"' in location
    assert '$uri !~ "^(/api/)"' in location
    # The carve-out is its own longer prefix location, so it beats `location /`.
    assert "location ^~ /api/admin { return 403; }" in rendered


def test_the_acme_challenge_path_is_served_under_a_path_allowlist():
    rendered = _render({"allowed_paths": ["/api/"], "allowed_methods": ["POST"]})
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    challenge_block = rendered[challenge:block_end]
    assert "$uri !~" not in challenge_block
    assert "$request_method !~" not in challenge_block
    assert '$uri !~ "^(/api/)"' in rendered
