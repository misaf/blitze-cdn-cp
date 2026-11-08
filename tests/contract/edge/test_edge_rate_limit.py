"""Per-site rate limits reaching the rendered configuration."""

from __future__ import annotations

import re

from support.edge_render import render_http_fragment, render_site
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible

REQUIRES_CAPABILITIES = {
    "test_a_site_without_a_limit_renders_no_zone": ("security",),
    "test_a_limit_renders_one_bounded_zone_and_applies_it_in_location_root": (
        "security",
    ),
    "test_the_key_map_exempts_allowed_sources_and_unlisted_paths": ("security",),
    "test_case_insensitive_paths_match_every_spelling": ("security",),
    "test_the_acme_challenge_path_is_never_limited": ("security",),
    "test_every_zone_a_site_names_is_declared": ("security",),
}


def _site(rate_limit, name="limited", **settings):
    return site_to_ansible(
        site_from_settings(
            {
                "name": name,
                "server_names": [f"{name}.example.com"],
                "origin_host": "origin.example.com",
                "rate_limit": rate_limit,
            }
            | settings
        )
    )


def _location_root(rendered):
    location = rendered[rendered.index("    location / {") :]
    return location[: location.index("try_files")]


def _key(snippet, *, exempt, uri):
    """What the site's key map yields, evaluated as Nginx's map would.

    Nginx tries the regex entries in order and falls back to the default; the
    patterns are plain PCRE, which Python's `re` reads the same way.
    """
    source_template = re.search(r'map "(.*)" \$blitzecdn_rate_limit_key_', snippet)
    assert source_template
    source = re.sub(
        r"\$blitzecdn_firewall_exempt_\w+", "1" if exempt else "0", source_template[1]
    ).replace("$uri", uri)
    for flags, pattern, value in re.findall(
        r'^\s+"~(\*?)(.*)" (\S+);$', snippet, flags=re.MULTILINE
    ):
        if re.search(pattern, source, re.IGNORECASE if flags else 0):
            return value
    return ""


def test_a_site_without_a_limit_renders_no_zone():
    plain = _site({})
    assert "limit_req_zone" not in render_http_fragment(
        "security-http.conf.j2", [plain]
    )
    assert "limit_req" not in _location_root(render_site(plain))


def test_a_limit_renders_one_bounded_zone_and_applies_it_in_location_root():
    site = _site({"requests": 30, "per": "minute", "burst": 10, "status": 503})
    snippet = render_http_fragment("security-http.conf.j2", [site])

    assert (
        "limit_req_zone $blitzecdn_rate_limit_key_limited "
        "zone=blitzecdn_rate_limit_limited:1m rate=30r/m;"
    ) in snippet
    location = _location_root(render_site(site))
    assert "limit_req zone=blitzecdn_rate_limit_limited burst=10 nodelay;" in location
    assert "limit_req_status 503;" in location
    assert "burst=" not in _location_root(render_site(_site({"requests": 5})))


def test_the_key_map_exempts_allowed_sources_and_unlisted_paths():
    """An empty key is neither counted nor limited by limit_req."""
    site = _site(
        {"requests": 5, "paths": ["/login", "/api/"]},
        firewall={"allow_sources": ["203.0.113.0/24"]},
    )
    snippet = render_http_fragment("security-http.conf.j2", [site])

    assert _key(snippet, exempt=False, uri="/login") == "$binary_remote_addr"
    assert _key(snippet, exempt=False, uri="/api/orders") == "$binary_remote_addr"
    assert _key(snippet, exempt=False, uri="/") == ""
    assert _key(snippet, exempt=False, uri="/LOGIN") == ""
    assert _key(snippet, exempt=True, uri="/login") == ""

    everywhere = render_http_fragment("security-http.conf.j2", [_site({"requests": 5})])
    assert _key(everywhere, exempt=False, uri="/anything") == "$binary_remote_addr"


def test_case_insensitive_paths_match_every_spelling():
    site = _site({"requests": 5, "paths": ["/login"], "case_insensitive_paths": True})
    snippet = render_http_fragment("security-http.conf.j2", [site])

    assert _key(snippet, exempt=False, uri="/LOGIN") == "$binary_remote_addr"
    assert _key(snippet, exempt=False, uri="/Login/form") == "$binary_remote_addr"


def test_the_acme_challenge_path_is_never_limited():
    rendered = render_site(_site({"requests": 1}))
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    assert "limit_req" not in rendered[challenge:block_end]


def test_every_zone_a_site_names_is_declared():
    """A zone named but never declared fails `nginx -t` on every edge."""
    limited = _site({"requests": 5}, name="example-com--api")
    plain = _site({}, name="plain")
    snippet = render_http_fragment("security-http.conf.j2", [limited, plain])

    declared = set(re.findall(r"zone=(blitzecdn_rate_limit_\w+):", snippet))
    used = set(
        re.findall(
            r"limit_req zone=(blitzecdn_rate_limit_\w+)",
            render_site(limited) + render_site(plain),
        )
    )
    assert declared == used == {"blitzecdn_rate_limit_example_com__api"}
