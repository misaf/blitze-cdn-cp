"""Edge TTL overrides and ignored origin headers, through the origin hop.

nginx's proxy_cache_valid and proxy_ignore_headers take no variables, so a
site with either setting reaches its origin through a loopback hop whose
location rewrites X-Accel-Expires, which nginx's cache reads before
Cache-Control and Expires. The maps that compute it are evaluated here the
way nginx does (first matching regex in order, else the default), so these
tests check the lifetime a response gets, not only that a directive exists.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError
from support.edge_render import render_http_fragment, render_site
from support.policy import site_from_settings

from blitzecdn.capabilities.cache.contract import CachePolicy
from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible

OVERRIDES = {"image/*": "7d", "text/html": "0", ".css": "1h"}


def _site(**cache: Any) -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": "shop-example",
                "server_names": ["shop.example.com"],
                "origin_host": "origin.example.com",
                **cache,
            }
        )
    )


def _map(http: str, variable: str) -> tuple[list[tuple[re.Pattern[str], str]], str]:
    match = re.search(
        r"^map \"[^\"]*\" \$" + re.escape(variable) + r" \{\n(.*?)^\}",
        http,
        re.M | re.S,
    )
    assert match, f"${variable} is not declared"
    body = match.group(1)
    assert "volatile;" in body, f"${variable} reads the response, so is volatile"
    default = re.search(r"^\s*default (\S+);", body, re.M)
    assert default
    entries = [
        (re.compile(pattern, re.I if flag == "*" else 0), value)
        for flag, pattern, value in re.findall(
            r'^\s*"~(\*?)(.+)" (\S+);\s*$', body, re.M
        )
    ]
    return entries, default.group(1)


def _evaluate(http: str, variable: str, source: str, origin_xae: str = "") -> str:
    """One map's value, with ``$1`` and the origin's X-Accel-Expires filled in."""
    if f"${variable} " not in http:
        return ""
    entries, default = _map(http, variable)
    # Exact strings are matched before any regex, as nginx does.
    body = http.split(f"${variable} {{", 1)[1].split("\n}", 1)[0]
    for exact, value in re.findall(r'^\s*"([^~"]*)" (\S+);\s*$', body, re.M):
        if exact == source:
            return value
    for pattern, value in entries:
        match = pattern.search(source)
        if match:
            return value.replace("$1", match.group(1) if match.groups() else "")
    return default.replace("$upstream_http_x_accel_expires", origin_xae).strip('"')


def _expires(
    site: dict[str, Any],
    *,
    uri: str,
    content_type: str,
    status: int = 200,
    cache_control: str = "",
    expires: str = "",
    origin_xae: str = "",
) -> str:
    """The X-Accel-Expires the hop hands the cache; empty sends none."""
    http = render_http_fragment("cache-http.conf.j2", [site])
    suffix = site["name"].replace("-", "_")
    response = f"{content_type}|{uri}"
    ttl = _evaluate(http, f"blitzecdn_cache_ttl_{suffix}", response)
    ignored = _evaluate(http, f"blitzecdn_cache_ignore_{suffix}", response) or "0"
    lifetime = _evaluate(
        http, "blitzecdn_cache_origin_lifetime", cache_control + expires + origin_xae
    )
    source = f"{status}:{ignored}:{lifetime}:{ttl}"
    return _evaluate(http, f"blitzecdn_cache_expires_{suffix}", source, origin_xae)


def _hop_servers(rendered: str) -> list[str]:
    return re.findall(
        r"^server \{\n    listen 127\.0\.0\.1:\d+;\n.*?^\}", rendered, re.M | re.S
    )


# --- the contract ---------------------------------------------------------


def test_both_settings_are_off_by_default():
    policy = CachePolicy()
    assert policy.cache_ttl_overrides == {}
    assert policy.cache_ignore_origin_headers == ()


def test_entries_are_trimmed_and_lowercased():
    policy = CachePolicy(
        cache_ttl_overrides={" Image/* ": "7d", ".CSS": "1h"},
        cache_ignore_origin_headers=(".JS", " .js", "image/*"),
    )
    assert policy.cache_ttl_overrides == {"image/*": "7d", ".css": "1h"}
    assert policy.cache_ignore_origin_headers == (".js", "image/*")


@pytest.mark.parametrize(
    "key",
    ["html", "*/*", "image/", "/png", ".", ".c ss", 'text/html"', "text/html;q=1"],
)
def test_an_entry_must_be_an_extension_type_or_family(key):
    """They reach an nginx regex inside a quoted string."""
    with pytest.raises(ValidationError):
        CachePolicy(cache_ttl_overrides={key: "1h"})
    with pytest.raises(ValidationError):
        CachePolicy(cache_ignore_origin_headers=(key,))


@pytest.mark.parametrize("ttl", ["500ms", "7", "1.5h", "-1s", "", "01h"])
def test_an_override_is_whole_seconds(ttl):
    """X-Accel-Expires counts seconds; a sub-second TTL would silently be 0."""
    with pytest.raises(ValidationError):
        CachePolicy(cache_ttl_overrides={"image/*": ttl})


def test_one_entry_named_twice_is_refused():
    with pytest.raises(ValidationError):
        CachePolicy(cache_ttl_overrides={".css": "1h", ".CSS": "2h"})


# --- routing --------------------------------------------------------------


def test_a_site_without_either_setting_reaches_its_origin_directly():
    """The hop costs a loopback round trip, so only sites that use it pay."""
    site = _site()
    rendered = render_site(site)
    http = render_http_fragment("cache-http.conf.j2", [site])

    assert "blitzecdn_origin_hop" not in rendered
    assert not _hop_servers(rendered)
    assert "$blitzecdn_cache_expires_" not in http


def test_a_site_that_does_not_cache_gets_no_hop():
    site = _site(cache_enabled=False, cache_ttl_overrides=OVERRIDES)
    assert "blitzecdn_origin_hop" not in render_site(site)
    http = render_http_fragment("cache-http.conf.j2", [site])
    assert "$blitzecdn_cache_expires_" not in http


@pytest.mark.parametrize(
    "cache",
    [
        {"cache_ttl_overrides": OVERRIDES},
        {"cache_ignore_origin_headers": ["image/*"]},
    ],
)
def test_every_listener_reaches_the_origin_through_its_own_hop(cache):
    site = _site(**cache)
    rendered = render_site(site)

    # Each visitor-facing proxy goes to the hop, under a Host naming the hop
    # server for its origin, and carries the Host the origin is to receive.
    hosts = re.findall(
        r"proxy_set_header Host (\S+\.hop\.blitzecdn\.invalid);", rendered
    )
    assert hosts
    assert rendered.count("proxy_pass http://blitzecdn_origin_hop;") == len(hosts)
    assert rendered.count("proxy_set_header BZ-Origin-Host $host;") == len(hosts)

    servers = _hop_servers(rendered)
    names = {re.search(r"server_name (\S+);", server).group(1) for server in servers}
    assert names == set(hosts)
    for server in servers:
        upstream = re.search(r"proxy_pass (https?)://(\S+);", server)
        assert upstream
        assert re.search(
            rf"^upstream {re.escape(upstream.group(2))} \{{", rendered, re.M
        )
        assert "proxy_set_header Host $http_bz_origin_host;" in server
        assert 'proxy_set_header BZ-Origin-Host "";' in server
        assert "limit_conn_dry_run on;" in server
        assert "proxy_hide_header X-Accel-Expires;" in server
        assert re.search(
            r"add_header X-Accel-Expires "
            r"\$blitzecdn_cache_expires_shop_example always;",
            server,
        )


def test_an_https_origin_is_verified_by_the_hop_not_the_listener():
    site = _site(
        cache_ttl_overrides=OVERRIDES,
        ssl_mode="full_strict",
        certificate_mode="existing",
        certificate_path="/etc/blitzecdn/tls/shop.pem",
        certificate_key_path="/etc/blitzecdn/tls/shop.key",
    )
    rendered = render_site(site)
    https_hops = [s for s in _hop_servers(rendered) if "proxy_pass https://" in s]
    assert https_hops
    for server in https_hops:
        assert "proxy_ssl_verify on;" in server
        assert "proxy_ssl_name origin.example.com;" in server
    # The visitor-facing proxies speak plain HTTP to the loopback hop.
    outside = rendered
    for server in _hop_servers(rendered):
        outside = outside.replace(server, "")
    assert "proxy_ssl_" not in outside


def test_the_hop_hands_the_origin_the_configured_host():
    site = _site(cache_ttl_overrides=OVERRIDES, origin_request_host="www.origin.test")
    rendered = render_site(site)
    assert "proxy_set_header BZ-Origin-Host www.origin.test;" in rendered


def test_every_variable_the_hop_names_is_declared():
    """An undeclared variable fails the whole edge's configuration test."""
    site = _site(cache_ttl_overrides=OVERRIDES, cache_ignore_origin_headers=[".js"])
    http = render_http_fragment("cache-http.conf.j2", [site])
    named = set(re.findall(r"\$blitzecdn_cache_\w+", render_site(site)))
    named |= set(re.findall(r"\$blitzecdn_cache_\w+", http))
    for variable in named:
        assert re.search(rf"^map .* {re.escape(variable)} \{{", http, re.M), variable


# --- the lifetime the cache reads -----------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        # The origin said nothing: the override is the lifetime.
        ({"uri": "/logo", "content_type": "image/png"}, "604800"),
        ({"uri": "/", "content_type": "text/html; charset=utf-8"}, "0"),
        ({"uri": "/app.css", "content_type": "text/css"}, "3600"),
        # An extension wins over the type the origin labels it with.
        ({"uri": "/odd.CSS", "content_type": "image/png"}, "3600"),
        # And an exact type over a family; `text/htmlx` is neither.
        ({"uri": "/", "content_type": "text/htmlx"}, ""),
        # A revalidation renews for as long as a fresh fetch would.
        ({"uri": "/logo", "content_type": "image/png", "status": 304}, "604800"),
        # 404 keeps cache_valid_not_found; other statuses keep nginx's rules.
        ({"uri": "/logo", "content_type": "image/png", "status": 404}, ""),
        ({"uri": "/logo", "content_type": "image/png", "status": 500}, ""),
        # No entry: nothing changes.
        ({"uri": "/api", "content_type": "application/json"}, ""),
        # The origin gave a lifetime of its own, so the origin decides.
        (
            {"uri": "/", "content_type": "text/html", "cache_control": "max-age=60"},
            "",
        ),
        (
            {"uri": "/logo", "content_type": "image/png", "cache_control": "no-store"},
            "",
        ),
        ({"uri": "/", "content_type": "text/html", "origin_xae": "120"}, "120"),
    ],
)
def test_an_override_applies_where_the_origin_is_silent(response, expected):
    assert _expires(_site(cache_ttl_overrides=OVERRIDES), **response) == expected


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        # Ignored, with an override: the override, whatever the origin said.
        (
            {"uri": "/logo", "content_type": "image/png", "cache_control": "no-cache"},
            "604800",
        ),
        (
            {"uri": "/logo", "content_type": "image/png", "origin_xae": "5"},
            "604800",
        ),
        # Ignored without one: the site's own durations, by status.
        (
            {
                "uri": "/app.js",
                "content_type": "text/javascript",
                "cache_control": "private",
            },
            "7200",
        ),
        ({"uri": "/app.js", "content_type": "text/javascript", "status": 404}, "300"),
        # Any other status is not stored: its origin headers are what is ignored.
        (
            {
                "uri": "/app.js",
                "content_type": "text/javascript",
                "status": 503,
                "cache_control": "max-age=3600",
            },
            "0",
        ),
        # A type not listed is still the origin's to decide.
        (
            {"uri": "/", "content_type": "text/html", "cache_control": "max-age=60"},
            "",
        ),
        (
            {"uri": "/", "content_type": "text/html", "origin_xae": "30"},
            "30",
        ),
    ],
)
def test_an_ignored_type_gets_the_edge_lifetime_whatever_the_origin_says(
    response, expected
):
    site = _site(
        cache_ttl_overrides={"image/*": "7d"},
        cache_ignore_origin_headers=["image/*", ".js"],
        cache_valid_success="2h",
        cache_valid_not_found="5m",
    )
    assert _expires(site, **response) == expected


def test_ignoring_headers_without_overrides_needs_no_ttl_map():
    site = _site(cache_ignore_origin_headers=["image/*"])
    http = render_http_fragment("cache-http.conf.j2", [site])
    assert "$blitzecdn_cache_ttl_" not in http
    assert (
        _expires(site, uri="/a", content_type="image/webp", cache_control="no-store")
        == "600"
    )
