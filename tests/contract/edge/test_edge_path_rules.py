"""Path rules as the edge renders them: ordered regex locations in each server."""

from __future__ import annotations

import re

from support.contracts import role_defaults
from support.edge_render import render_http_fragment, render_site, server_blocks

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, Rule, derive_hosts

REQUIRES_CAPABILITIES = {
    "test_a_path_scope_reads_its_own_cookie_map": ("cache",),
    "test_a_path_location_carries_its_cache_settings": ("cache",),
}

TLS = {
    "ssl_mode": "flexible",
    "certificate_mode": "existing",
    "certificate_path": "/etc/ssl/certs/edge.pem",
    "certificate_key_path": "/etc/ssl/private/edge.key",
}


def _site(*rules: dict, records=("www", "shop"), **zone: object) -> dict:
    domain = Domain.model_validate({"name": "example.com", "policy": {}})
    if zone:
        domain = Domain(name="example.com", policy=domain.policy.applying(zone))
    (host,) = derive_hosts(
        [domain],
        [Rule.model_validate({"domain": "example.com", **rule}) for rule in rules],
        [
            DnsRecord(domain="example.com", name=name, value="198.51.100.10")
            for name in records
        ],
    )
    return site_to_ansible(host)


def _locations(block: str) -> list[str]:
    return re.findall(r"^    location (\S+(?: \S+)?) \{", block, re.M)


def test_path_rules_render_in_priority_order_before_the_catch_all():
    """Regex locations are tried in the order written, so priority holds."""
    site = _site(
        {
            "name": "wide",
            "priority": 20,
            "path": "/static/",
            "overrides": {"cache_valid_success": "1h"},
        },
        {
            "name": "narrow",
            "priority": 10,
            "path": "/static/img/",
            "overrides": {"cache_valid_success": "1d"},
        },
        {
            "name": "robots",
            "priority": 30,
            "path": "/robots.txt",
            "redirect": {"target": "/robots-new.txt"},
        },
    )

    http, _ = server_blocks(render_site(site), role_defaults())

    for block in http:
        order = _locations(block)
        assert order.index('~ "^/static/img/"') < order.index('~ "^/static/"')
        assert order.index('~ "^/static/"') < order.index('~ "^/robots\\.txt$"')
        assert order.index('~ "^/robots\\.txt$"') < order.index("/")
        assert order.index("^~ /.well-known/acme-challenge/") < order.index(
            '~ "^/static/img/"'
        )


def test_a_location_is_rendered_only_on_the_hostnames_its_match_covers():
    site = _site(
        {
            "name": "shop-only",
            "match": "shop.example.com",
            "path": "/cart/",
            "overrides": {"cache_enabled": False},
        },
    )

    http, _ = server_blocks(render_site(site), role_defaults())

    by_host = {
        re.search(r"server_name (\S+);", block).group(1): block for block in http
    }
    assert "@blitzecdn_path_shop-only" in by_host["shop.example.com"]
    assert "@blitzecdn_path_shop-only" not in by_host["www.example.com"]


def test_a_redirect_takes_its_suffix_from_the_raw_request_uri():
    """$uri is decoded, so a capture of it could put a CR or LF in Location."""
    site = _site(
        {
            "name": "old",
            "path": "/old/",
            "redirect": {"target": "https://new.example.net/$1", "status": 308},
        },
    )

    rendered = render_site(site)

    assert 'if ($request_uri ~ "^/old/([^?]*)") {' in rendered
    assert 'return 308 "https://new.example.net/$1$is_args$args";' in rendered
    assert 'return 308 "https://new.example.net/$is_args$args";' in rendered
    assert "@blitzecdn_path_old" not in rendered


def test_a_redirect_can_drop_the_query_string():
    site = _site(
        {
            "name": "gone",
            "path": "/gone.html",
            "redirect": {"target": "/", "preserve_query_string": False, "status": 302},
        },
    )

    rendered = render_site(site)

    assert 'location ~ "^/gone\\.html$" {' in rendered
    assert 'return 302 "/";' in rendered
    assert "$request_uri ~" not in rendered


def test_a_path_location_carries_its_cache_settings():
    site = _site(
        {
            "name": "assets",
            "path": "/static/",
            "overrides": {"cache_valid_success": "7d", "cache_level": "everything"},
        },
    )

    rendered = render_site(site)
    path_block = rendered.split("location @blitzecdn_path_assets {", 1)[1].split(
        "\n    }\n", 1
    )[0]
    host_block = rendered.split("location @blitzecdn_upstream {", 1)[1].split(
        "\n    }\n", 1
    )[0]

    assert "proxy_cache_valid 200 301 302 7d;" in path_block
    assert "proxy_cache_valid 200 301 302 10m;" in host_block
    assert "proxy_pass http://blitzecdn_origin_example-com_http_80;" in path_block


def test_a_path_scope_reads_its_own_cookie_map():
    site = _site(
        {
            "name": "assets",
            "path": "/static/",
            "overrides": {"cache_bypass_cookies": ["session"]},
        },
    )

    rendered = render_site(site)
    http = render_http_fragment("cache-http.conf.j2", [site])

    path_block = rendered.split("location @blitzecdn_path_assets {", 1)[1]
    assert "$blitzecdn_cache_cookie_example_com_path_assets" in path_block
    assert "$blitzecdn_cache_cookie_example_com_path_assets {" in http
    assert "$blitzecdn_cache_cookie_example_com {" in http


def test_a_path_location_writes_its_own_visitor_headers():
    site = _site(
        {
            "name": "private",
            "path": "/private/",
            "overrides": {"visitor_headers": {"connecting_ip": False}},
        },
    )

    rendered = render_site(site)
    path_block = rendered.split("location @blitzecdn_path_private {", 1)[1].split(
        "\n    }\n", 1
    )[0]
    host_block = rendered.split("location @blitzecdn_upstream {", 1)[1].split(
        "\n    }\n", 1
    )[0]

    assert 'proxy_set_header BZ-Connecting-IP "";' in path_block
    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in host_block


def test_an_http_listener_that_redirects_to_https_renders_no_path_rules():
    site = _site(
        {"name": "old", "path": "/old/", "redirect": {"target": "/new/"}},
        records=("www",),
        always_use_https=True,
        **TLS,
    )

    http, https = server_blocks(render_site(site), role_defaults())

    assert all('location ~ "^/old/"' not in block for block in http)
    assert all('location ~ "^/old/"' in block for block in https)
