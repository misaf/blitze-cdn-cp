"""Per-site rate limits reaching the rendered configuration."""

from __future__ import annotations

import re

from support.edge_render import render_http_fragment, render_site
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.security.contract import rate_limit_zone

REQUIRES_CAPABILITIES = {
    "test_a_rate_limit_renders_into_the_site_location": ("security",),
    "test_a_site_without_a_rate_limit_renders_no_limit": ("security",),
    "test_allowed_sources_and_unlisted_paths_are_not_counted": ("security",),
    "test_without_exemptions_or_paths_every_request_counts": ("security",),
    "test_the_acme_challenge_path_is_never_rate_limited": ("security",),
    "test_sites_on_one_rate_share_one_zone": ("security",),
    "test_every_zone_a_site_reads_is_declared": ("security",),
}


def _site(*, rate_limit=None, firewall=None, name="limited"):
    settings = {
        "name": name,
        "server_names": [f"{name}.example.com"],
        "origin_host": "origin.example.com",
    }
    if rate_limit is not None:
        settings["rate_limit"] = rate_limit
    if firewall is not None:
        settings["firewall"] = firewall
    return site_to_ansible(site_from_settings(settings))


def _location(rendered):
    location = rendered[rendered.index("    location / {") :]
    return location[: location.index("try_files")]


def test_a_rate_limit_renders_into_the_site_location():
    rendered = render_site(
        _site(rate_limit={"rate": "10r/s", "burst": 20, "status": 503})
    )
    location = _location(rendered)
    assert (
        "set $blitzecdn_rate_limit_key $blitzecdn_rate_limit_key_limited;" in location
    )
    assert "limit_req zone=blitzecdn_rate_10rs burst=20 nodelay;" in location
    assert "limit_req_status 503;" in location
    # The key is set in the rewrite phase, before limit_req reads it.
    assert location.index("set $blitzecdn_rate_limit_key") < location.index(
        "limit_req zone="
    )


def test_a_site_without_a_rate_limit_renders_no_limit():
    site = _site()
    assert "rate_limit" not in site
    rendered = render_site(site)
    assert "$blitzecdn_rate_limit_key" not in rendered
    # Only the Under Attack verify endpoint may carry a limit.
    assert "zone=blitzecdn_rate_" not in rendered
    assert "limit_req_zone" not in render_http_fragment("security-http.conf.j2", [site])


def test_allowed_sources_and_unlisted_paths_are_not_counted():
    """An empty key is not counted: the site's map clears it for both.

    Never `if … set` in the location: a matching `if` loses its try_files, so
    every exempt client and unlisted path would get a 404 (seen under nginx).
    """
    site = _site(
        rate_limit={"rate": "10r/m", "paths": ["/login", "/api/"]},
        firewall={"allow_sources": ["203.0.113.0/24"]},
    )
    snippet = render_http_fragment("security-http.conf.j2", [site])
    assert (
        'map "$blitzecdn_firewall_exempt_limited:$uri" '
        "$blitzecdn_rate_limit_key_limited {"
    ) in snippet
    assert '"~^0:(/login$|/api/)" "limited:$binary_remote_addr";' in snippet
    location = _location(render_site(site))
    assert "if (" not in location
    assert "limit_req zone=blitzecdn_rate_10rm nodelay;" in location
    assert "limit_req_status 429;" in location


def test_without_exemptions_or_paths_every_request_counts():
    site = _site(rate_limit={"rate": "1r/s"}, name="whole-site")
    snippet = render_http_fragment("security-http.conf.j2", [site])
    assert 'map "0:$uri" $blitzecdn_rate_limit_key_whole_site {' in snippet
    assert '"~^0:" "whole-site:$binary_remote_addr";' in snippet


def test_the_acme_challenge_path_is_never_rate_limited():
    rendered = render_site(_site(rate_limit={"rate": "1r/s"}))
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    assert "limit_req" not in rendered[challenge:block_end]


def test_sites_on_one_rate_share_one_zone():
    """Zones are bounded by the rate ladder, not by the number of sites."""
    sites = [
        _site(rate_limit={"rate": "10r/s"}, name="one"),
        _site(rate_limit={"rate": "10r/s"}, name="two"),
        _site(rate_limit={"rate": "30r/m"}, name="three"),
        _site(name="four"),
    ]
    snippet = render_http_fragment("security-http.conf.j2", sites)
    zones = re.findall(r"limit_req_zone \S+ zone=(\w+):5m rate=(\S+);", snippet)
    assert sorted(zones) == [
        ("blitzecdn_rate_10rs", "10r/s"),
        ("blitzecdn_rate_30rm", "30r/m"),
    ]
    # The key every zone reads is declared even where no server sets it.
    assert 'map "" $blitzecdn_rate_limit_key {' in snippet


def test_every_zone_a_site_reads_is_declared():
    """A zone read but never declared fails `nginx -t` on every edge."""
    sites = [
        _site(rate_limit={"rate": rate}, name=f"site{index}")
        for index, rate in enumerate(("1r/s", "1000r/s", "10r/m"))
    ]
    snippet = render_http_fragment("security-http.conf.j2", sites)
    declared = set(re.findall(r"zone=(blitzecdn_rate_\w+):", snippet))
    keys = set(re.findall(r"map \S+ (\$blitzecdn_rate_limit_key_\w+) \{", snippet))
    read = set(
        re.findall(
            r"limit_req zone=(blitzecdn_rate_\w+)",
            "".join(render_site(site) for site in sites),
        )
    )
    assert read == declared
    assert keys == {f"$blitzecdn_rate_limit_key_site{index}" for index in range(3)}
    assert declared == {rate_limit_zone(rate) for rate in ("1r/s", "1000r/s", "10r/m")}
