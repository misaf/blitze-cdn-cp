"""Rules that match a request path as well as a hostname.

Matching is hostname, then path: a hostname rule decides the hostname's
policy, and the first path rule covering a request layers on top of it. A path
rule overrides only path-scoped settings, or redirects.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.cache.contract import CachePolicy
from blitzecdn.capabilities.sites.adapters.ansible import (
    site_to_ansible,
    variable_scope,
)
from blitzecdn.capabilities.sites.domain import (
    DnsRecord,
    Domain,
    Rule,
    derive_hosts,
    resolve_policy,
)
from blitzecdn.capabilities.sites.domain.hosts import host_source, unservable_hosts
from blitzecdn.core.domain.paths import PathRedirect, path_matches, path_pattern
from blitzecdn.core.domain.site_policy import SitePolicy

ZONE = Domain(name="example.com")


def _rule(name: str, **fields: object) -> Rule:
    return Rule.model_validate({"domain": "example.com", "name": name, **fields})


def _record(name: str, value: str = "198.51.100.10") -> DnsRecord:
    return DnsRecord(domain="example.com", name=name, value=value)


# -- What a path rule may say -----------------------------------------------


def test_cache_and_visitor_headers_are_the_path_scoped_settings():
    assert SitePolicy.path_setting_names() == {
        *CachePolicy.path_settings,
        "visitor_headers",
    }


@pytest.mark.parametrize(
    ("path", "covered", "not_covered"),
    [
        ("/static/", "/static/app.css", "/static"),
        ("/static/", "/static/", "/staticfiles/x"),
        ("/robots.txt", "/robots.txt", "/robots.txt/x"),
    ],
)
def test_a_trailing_slash_is_a_prefix_and_anything_else_is_exact(
    path, covered, not_covered
):
    assert path_matches(path, covered)
    assert not path_matches(path, not_covered)


def test_the_rendered_pattern_anchors_both_forms():
    assert path_pattern("/static/") == "^/static/"
    assert path_pattern("/robots.txt") == r"^/robots\.txt$"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("static/", "must start with '/'"),
        ("/a b/", "may not contain whitespace"),
        ('/a"b', "may not contain whitespace"),
        ("/a;b", "may not contain whitespace"),
        ("/a?b=1", "may not contain '.' or '#'"),
        ("/" + "a" * 512, "at most 512"),
    ],
)
def test_a_path_that_could_escape_its_nginx_string_is_refused(path, message):
    with pytest.raises(ValidationError, match=message):
        _rule("bad", path=path, overrides={"cache_enabled": False})


def test_a_path_rule_may_not_override_a_hostname_setting():
    with pytest.raises(ValidationError, match=r"decided per hostname.*ssl_mode"):
        _rule("tls", path="/admin/", overrides={"ssl_mode": "off"})


def test_a_path_rule_must_do_something():
    with pytest.raises(ValidationError, match="override at least one setting or"):
        _rule("empty", path="/x/")


def test_a_path_rule_redirects_or_overrides_but_not_both():
    with pytest.raises(ValidationError, match="not both"):
        _rule(
            "both",
            path="/old/",
            overrides={"cache_enabled": False},
            redirect={"target": "/new/"},
        )


def test_only_a_path_rule_can_redirect():
    with pytest.raises(ValidationError, match="only a path rule can redirect"):
        _rule("host", redirect={"target": "/new/"})


def test_a_redirect_suffix_needs_a_prefix_path():
    with pytest.raises(ValidationError, match=r"\$1 only on a prefix path"):
        _rule("exact", path="/old.html", redirect={"target": "/new/$1"})


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("new/", "absolute http:// or https:// URL"),
        ("ftp://example.net/", "absolute http:// or https:// URL"),
        ("/new/$host", "no other '\\$' variable"),
        ("/new/$10", "followed by a digit"),
        ("/new/{x}", "may not contain"),
        ("/new/#top", "fragment"),
    ],
)
def test_a_redirect_target_cannot_inject_nginx(target, message):
    with pytest.raises(ValidationError, match=message):
        PathRedirect(target=target)


def test_a_target_with_a_query_cannot_also_preserve_the_visitors():
    with pytest.raises(ValidationError, match="its own query string"):
        PathRedirect(target="/new/?from=old")
    assert PathRedirect(target="/new/?from=old", preserve_query_string=False)


# -- Resolution -------------------------------------------------------------


def test_a_hostname_alone_ignores_path_rules():
    rules = [
        _rule(
            "static",
            priority=1,
            path="/static/",
            overrides={"cache_valid_success": "1d"},
        )
    ]

    resolved = resolve_policy(ZONE, rules, "www.example.com")

    assert resolved.rule is None
    assert resolved.path_rule is None
    assert resolved.policy == ZONE


def test_a_path_rule_layers_on_the_hostnames_policy():
    """The hostname rule's overrides survive under the path; first match wins."""
    rules = [
        _rule(
            "api",
            priority=50,
            match="api.example.com",
            overrides={"cache_level": "everything"},
        ),
        _rule(
            "assets",
            priority=10,
            path="/static/",
            overrides={"cache_valid_success": "1d"},
        ),
        _rule(
            "later",
            priority=20,
            path="/static/",
            overrides={"cache_valid_success": "1h"},
        ),
    ]

    resolved = resolve_policy(ZONE, rules, "api.example.com", "/static/app.css")

    assert (resolved.rule, resolved.path_rule) == ("api", "assets")
    cache = resolved.policy.policy[CachePolicy]
    assert cache.cache_level == "everything"
    assert cache.cache_valid_success == "1d"

    elsewhere = resolve_policy(ZONE, rules, "api.example.com", "/index.html")
    assert (elsewhere.rule, elsewhere.path_rule) == ("api", None)
    assert elsewhere.policy.policy[CachePolicy].cache_valid_success == "10m"


def test_a_redirecting_path_rule_is_reported_with_the_hostnames_policy():
    rules = [_rule("old", path="/old/", redirect={"target": "/new/$1", "status": 308})]

    resolved = resolve_policy(ZONE, rules, "www.example.com", "/old/a")

    assert resolved.path_rule == "old"
    assert resolved.redirect == PathRedirect(target="/new/$1", status=308)
    assert resolved.policy == ZONE


def test_a_disabled_path_rule_matches_nothing():
    rules = [
        _rule("off", path="/x/", overrides={"cache_enabled": False}, enabled=False)
    ]

    assert resolve_policy(ZONE, rules, "www.example.com", "/x/y").path_rule is None


# -- Derivation -------------------------------------------------------------


def test_path_rules_become_locations_and_never_hosts():
    rules = [
        _rule(
            "assets",
            priority=10,
            path="/static/",
            overrides={"cache_valid_success": "1d"},
        ),
        _rule(
            "old",
            priority=20,
            match="www.example.com",
            path="/old/",
            redirect={"target": "/new/$1"},
        ),
    ]

    hosts = derive_hosts([ZONE], rules, [_record("www"), _record("shop")])

    assert [host.name for host in hosts] == ["example-com"]
    (host,) = hosts
    assert [location.rule for location in host.locations] == ["assets", "old"]
    assets, old = host.locations
    assert assets.server_names == ("www.example.com", "shop.example.com")
    assert assets.settings["cache_valid_success"] == "1d"
    assert assets.settings["cache_enabled"] is True
    assert set(assets.settings) == SitePolicy.path_setting_names()
    # A narrower match names only the hostnames it covers.
    assert old.server_names == ("www.example.com",)
    assert old.redirect is not None
    assert not old.settings


def test_a_path_rule_layers_on_each_hosts_own_policy():
    rules = [
        _rule(
            "api",
            priority=50,
            match="api.example.com",
            overrides={"cache_level": "everything"},
        ),
        _rule(
            "assets",
            priority=10,
            path="/static/",
            overrides={"cache_valid_success": "1d"},
        ),
    ]

    hosts = {
        host.name: host
        for host in derive_hosts([ZONE], rules, [_record("www"), _record("api")])
    }

    assert hosts["example-com"].locations[0].settings["cache_level"] == "standard"
    assert (
        hosts["example-com--api"].locations[0].settings["cache_level"] == "everything"
    )


def test_host_source_never_names_a_path_rule():
    rules = [_rule("assets", path="/static/", overrides={"cache_enabled": False})]

    assert host_source([ZONE], rules, "example-com--assets") == ("example.com", None)


def test_the_edge_document_names_each_locations_variable_scope():
    rules = [_rule("assets", path="/static/", overrides={"cache_valid_success": "1d"})]
    (host,) = derive_hosts([ZONE], rules, [_record("www")])

    (location,) = site_to_ansible(host)["locations"]

    assert location["variable_scope"] == variable_scope("example-com", "assets")
    assert location["variable_scope"] != variable_scope("example-com", "other")
    assert location["path"] == "/static/"
    assert location["pattern"] == path_pattern("/static/")


def test_a_host_without_path_rules_carries_no_locations_to_the_edge():
    (host,) = derive_hosts([ZONE], [], [_record("www")])

    assert "locations" not in site_to_ansible(host)


def test_query_string_mode_stays_per_hostname():
    """A purge rebuilds the cache key from the host's policy alone."""
    with pytest.raises(ValidationError, match="cache_query_string_mode"):
        _rule("q", path="/static/", overrides={"cache_query_string_mode": "ignore"})


def test_a_path_rule_narrower_than_a_wildcard_server_is_refused_by_name():
    """The wildcard server renders once for every name, so it cannot be honoured."""
    rules = [
        _rule(
            "shop",
            match="shop.example.com",
            path="/cart/",
            overrides={"cache_enabled": False},
        )
    ]
    records = [_record("*")]

    assert derive_hosts([ZONE], rules, records) == []
    (refused,) = unservable_hosts([ZONE], rules, records)
    assert "path rule 'shop'" in refused.reason
    assert "*.example.com" in refused.reason


def test_a_path_rule_matching_the_wildcard_itself_is_served():
    rules = [
        _rule(
            "all",
            match="*.example.com",
            path="/cart/",
            overrides={"cache_enabled": False},
        )
    ]

    (host,) = derive_hosts([ZONE], rules, [_record("*")])

    assert host.locations[0].server_names == ("*.example.com",)


def test_a_location_can_require_a_capability_its_host_does_not():
    rules = [
        _rule(
            "country", path="/geo/", overrides={"visitor_headers": {"ip_country": True}}
        )
    ]
    (host,) = derive_hosts([ZONE], rules, [_record("www")])

    assert "geoip" not in host.policy.capability_requirements
    assert host.capability_requirements["geoip"] == (
        "visitor_headers.ip_country (path rule 'country')",
    )
