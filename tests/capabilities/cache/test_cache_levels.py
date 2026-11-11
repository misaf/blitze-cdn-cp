"""Cache levels and bypass cookies: what a site lets the shared cache keep.

The rendered maps are evaluated here the way nginx evaluates them (first
matching regex in order, else the default), so these tests check what a
request gets, not only that a directive is there. nginx's PCRE and Python's
``re`` agree on every construct the maps use.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError
from support.edge_render import render_http_fragment, render_site
from support.policy import site_from_settings

from blitzecdn.capabilities.cache.contract import CacheLevel, CachePolicy
from blitzecdn.capabilities.sites.adapters.ansible import site_to_ansible


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
    """The regex entries and default of the map that defines ``variable``."""
    match = re.search(
        r"^map \"[^\"]*\" \$" + re.escape(variable) + r" \{\n(.*?)^\}",
        http,
        re.M | re.S,
    )
    assert match, f"${variable} is not declared"
    body = match.group(1)
    default = re.search(r"^\s*default (\S+);", body, re.M)
    assert default
    entries = [
        (re.compile(pattern, re.I if flag == "*" else 0), value)
        for flag, pattern, value in re.findall(
            r'^\s*"~(\*?)(.+)" (\S+);\s*$', body, re.M
        )
    ]
    return entries, default.group(1)


def _evaluate(http: str, variable: str, source: str) -> str:
    entries, default = _map(http, variable)
    for pattern, value in entries:
        if pattern.search(source):
            return value
    return default


def _static(http: str, uri: str) -> str:
    match = re.search(r"map \$uri \$blitzecdn_cache_static_uri \{(.*?)\}", http, re.S)
    assert match
    pattern = re.search(r'"~\*(.+)" 1;', match.group(1))
    assert pattern
    return "1" if re.search(pattern.group(1), uri, re.I) else "0"


def _bypassed(site: dict[str, Any], uri: str, cookie: str) -> bool:
    """Whether the site's cookie map keeps this request away from the cache."""
    http = render_http_fragment("cache-http.conf.j2", [site])
    variable = "blitzecdn_cache_cookie_" + site["name"].replace("-", "_")
    if f"${variable} " not in http:
        return False
    return _evaluate(http, variable, f"{_static(http, uri)}:{cookie}") == "1"


def _not_stored(uri: str, content_type: str, *lifetime: str) -> bool:
    """`$blitzecdn_cache_standard_skip` for one response to a standard site."""
    http = render_http_fragment("cache-http.conf.j2", [_site()])
    source = f"{_static(http, uri)}:{content_type}:{''.join(lifetime)}"
    return _evaluate(http, "blitzecdn_cache_standard_skip", source) == "1"


def test_the_default_level_is_standard_with_no_named_cookies():
    """Closes the leak by default; `everything` is the opt-in."""
    policy = CachePolicy()
    assert policy.cache_level is CacheLevel.STANDARD
    assert policy.cache_bypass_cookies == ()


@pytest.mark.parametrize("bad", ["", "a b", "session;x", 'a"b', "a=b", "x" * 129])
def test_a_bypass_cookie_must_be_a_plain_name(bad):
    """The names reach an nginx regex inside a quoted string."""
    with pytest.raises(ValidationError):
        CachePolicy(cache_bypass_cookies=(bad,))


def test_bypass_cookies_are_trimmed_and_deduplicated_in_order():
    policy = CachePolicy(cache_bypass_cookies=(" sid ", "wp_*", "sid"))
    assert policy.cache_bypass_cookies == ("sid", "wp_*")


@pytest.mark.parametrize(
    ("uri", "cookie", "bypassed"),
    [
        # The #168 failure scenario: a logged-in visitor's page.
        ("/account", "session=abc", True),
        ("/account", "", False),
        # Browsers send cookies with every asset; those stay cacheable.
        ("/assets/app.css", "session=abc", False),
        ("/img/Logo.PNG", "session=abc", False),
    ],
)
def test_standard_skips_the_cache_for_any_cookie_on_a_dynamic_url(
    uri, cookie, bypassed
):
    assert _bypassed(_site(), uri, cookie) is bypassed


@pytest.mark.parametrize(
    ("cookie", "bypassed"),
    [
        ("_ga=GA1.2.3", False),
        ("wordpress_logged_in_abc123=x", True),
        ("_ga=1; wordpress_logged_in_abc123=x", True),
        ("_ga=1;wordpress_logged_in_=x", True),
        ("sid=1", True),
        # A name containing a listed one is a different cookie.
        ("notsid=1", False),
        ("xwordpress_logged_in_a=1", False),
        # A listed name in a value is not that cookie.
        ("_ga=sid=1", False),
    ],
)
def test_named_cookies_narrow_what_skips_the_cache(cookie, bypassed):
    site = _site(cache_bypass_cookies=["wordpress_logged_in_*", "sid"])
    assert _bypassed(site, "/account", cookie) is bypassed


def test_under_standard_a_static_url_is_cached_even_with_a_named_cookie():
    site = _site(cache_bypass_cookies=["sid"])
    assert _bypassed(site, "/app.js", "sid=1") is False


def test_everything_keeps_every_cookie_request_cacheable_unless_named():
    """The pre-#168 behavior, now opt-in, with no cookie map at all."""
    site = _site(cache_level="everything")
    http = render_http_fragment("cache-http.conf.j2", [site])
    upstream = render_site(site)

    assert "$blitzecdn_cache_cookie_" not in http
    assert "$blitzecdn_cache_cookie_" not in upstream
    assert "proxy_cache_bypass $http_upgrade $http_authorization;" in upstream
    assert "proxy_no_cache $http_upgrade $http_authorization;" in upstream


def test_everything_with_named_cookies_bypasses_on_every_url():
    site = _site(cache_level="everything", cache_bypass_cookies=["sid"])
    assert _bypassed(site, "/app.js", "sid=1") is True
    assert _bypassed(site, "/account", "sid=1") is True
    assert _bypassed(site, "/account", "other=1") is False


@pytest.mark.parametrize(
    ("cache", "bypass", "no_cache"),
    [
        (
            {},
            "$http_upgrade $http_authorization $blitzecdn_cache_cookie_shop_example",
            "$http_upgrade $http_authorization $blitzecdn_cache_cookie_shop_example "
            "$blitzecdn_cache_standard_skip",
        ),
        (
            {"cache_bypass_cookies": ["sid"]},
            "$http_upgrade $http_authorization $blitzecdn_cache_cookie_shop_example",
            "$http_upgrade $http_authorization $blitzecdn_cache_cookie_shop_example "
            "$blitzecdn_cache_standard_skip",
        ),
        (
            {"cache_level": "everything"},
            "$http_upgrade $http_authorization",
            "$http_upgrade $http_authorization",
        ),
        (
            {"cache_level": "everything", "cache_bypass_cookies": ["sid"]},
            "$http_upgrade $http_authorization $blitzecdn_cache_cookie_shop_example",
            "$http_upgrade $http_authorization $blitzecdn_cache_cookie_shop_example",
        ),
    ],
)
def test_each_level_names_its_variables_in_both_directives(cache, bypass, no_cache):
    """A cookie stored but not served, or served but not stored, still leaks."""
    site = _site(**cache)
    upstream = render_site(site)
    http = render_http_fragment("cache-http.conf.j2", [site])

    assert f"proxy_cache_bypass {bypass};" in upstream
    assert f"proxy_no_cache {no_cache};" in upstream
    # Every variable the site names is one the http fragment declares;
    # otherwise the edge refuses the whole configuration.
    for variable in set(re.findall(r"\$blitzecdn_cache_\w+", upstream)):
        assert re.search(rf"^map .* {re.escape(variable)} \{{", http, re.M), variable


def test_a_site_that_does_not_cache_declares_no_cookie_map():
    site = _site(cache_enabled=False)
    http = render_http_fragment("cache-http.conf.j2", [site])
    assert "$blitzecdn_cache_cookie_" not in http
    assert "proxy_cache " not in render_site(site)


@pytest.mark.parametrize(
    ("uri", "content_type", "lifetime", "skipped"),
    [
        # HTML the origin said nothing about: the leak `standard` closes.
        ("/account", "text/html; charset=utf-8", (), True),
        ("/api/cart", "application/json", (), True),
        # The origin gave it a lifetime, so the origin decided.
        ("/account", "text/html", ("public, max-age=60",), False),
        ("/news", "text/html", ("", "Thu, 01 Jan 2099 00:00:00 GMT"), False),
        ("/news", "text/html", ("", "", "120"), False),
        # Static by extension, or by type when the URL does not say.
        ("/app.css", "text/html", (), False),
        ("/avatar", "image/png", (), False),
        ("/bundle", "application/javascript; charset=utf-8", (), False),
        ("/styles", "TEXT/CSS", (), False),
        ("/fonts/x", "font/woff2", (), False),
        # A type merely starting with a static one's name is not one.
        ("/data", "text/csv", (), True),
    ],
)
def test_standard_stores_only_static_types_unless_the_origin_sets_a_lifetime(
    uri, content_type, lifetime, skipped
):
    assert _not_stored(uri, content_type, *lifetime) is skipped


def test_the_response_dependent_map_is_volatile():
    """It reads $upstream_http_*; a value cached before the response is wrong."""
    http = render_http_fragment("cache-http.conf.j2", [_site()])
    body = http.split("$blitzecdn_cache_standard_skip {", 1)[1].split("}", 1)[0]
    assert "volatile;" in body


def test_the_response_dependent_map_is_never_read_before_the_response():
    upstream = render_site(_site())
    bypass = re.search(r"proxy_cache_bypass [^;]*;", upstream)
    assert bypass
    assert "$blitzecdn_cache_standard_skip" not in bypass.group(0)
