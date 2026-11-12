"""The security capability: firewall rules and Under Attack Mode as one owner."""

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.security.contract import (
    SecurityPolicy,
    SiteFirewall,
    SiteRateLimit,
)


def test_the_firewall_and_under_attack_mode_share_one_policy():
    policy = SecurityPolicy(
        under_attack_mode=True, firewall={"denied_countries": ["DE"]}
    )

    assert policy.under_attack_mode
    assert policy.firewall.requires_geoip
    assert not SecurityPolicy().firewall.requires_geoip
    assert SecurityPolicy().firewall.empty


def test_country_rules_are_validated_where_they_are_owned():
    with pytest.raises(ValidationError, match="ISO 3166-1 alpha-2"):
        SiteFirewall(denied_countries=("UK",))
    with pytest.raises(ValidationError, match="both list"):
        SiteFirewall(allowed_countries=("DE",), denied_countries=("DE",))
    assert SiteFirewall(denied_countries=("de",)).denied_countries == ("DE",)


def test_every_rule_kind_is_validated_and_normalised_by_the_contract():
    """The contract owns its validation vocabulary; assert it from the outside."""
    firewall = SiteFirewall(
        allow_sources=(" 198.51.100.0/24 ",),
        deny_sources=("203.0.113.7",),
        allowed_countries=(" de ",),
        denied_methods=("delete",),
        denied_paths=("/wp-admin",),
    )

    assert firewall.allow_sources == ("198.51.100.0/24",)
    assert firewall.deny_sources == ("203.0.113.7/32",)
    assert firewall.allowed_countries == ("DE",)
    assert firewall.denied_methods == ("DELETE",)
    assert firewall.denied_paths == ("/wp-admin",)


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ({"allow_sources": ("198.51.100.1/24",)}, "not an IP address or CIDR network"),
        ({"deny_sources": ("not-an-address",)}, "not an IP address or CIDR network"),
        ({"allow_sources": ("10.0.0.0/8", "10.0.0.0/8")}, "duplicate source"),
        ({"allow_sources": ("fe80::1%x; return 200 x",)}, "zone indexes"),
        ({"deny_sources": ("fe80::%eth0/64",)}, "zone indexes"),
        ({"denied_countries": ("ZZ",)}, "ISO 3166-1 alpha-2"),
        ({"denied_countries": ("UK",)}, "use 'GB'"),
        ({"denied_methods": ("GET IT",)}, "not an HTTP method"),
        ({"denied_paths": ("wp-admin",)}, "must start with '/'"),
        ({"denied_paths": ("/a b",)}, "may not contain whitespace"),
        ({"denied_paths": ("/x;y",)}, "may not contain whitespace"),
    ],
)
def test_a_rule_the_edge_could_not_render_is_refused(rules, message):
    with pytest.raises(ValidationError, match=message):
        SiteFirewall(**rules)


def test_an_unconfigured_firewall_is_empty_and_a_configured_one_is_not():
    assert SiteFirewall().empty
    assert not SiteFirewall(denied_paths=("/admin",)).empty
    assert SecurityPolicy().required_capabilities == frozenset()
    assert SecurityPolicy(firewall={"denied_paths": ("/admin",)}).required_capabilities


def test_case_insensitive_matching_alone_filters_nothing():
    """The flag only changes how path rules match; without them it is a no-op."""
    assert not SiteFirewall().case_insensitive_paths
    flag_only = SiteFirewall(case_insensitive_paths=True)
    assert not flag_only.empty, "kept, so rate_limit.paths match through it"
    assert flag_only.rule_count == 0
    assert SecurityPolicy(firewall=flag_only).required_capabilities == frozenset()
    with_paths = SiteFirewall(denied_paths=("/admin",), case_insensitive_paths=True)
    assert not with_paths.empty
    assert with_paths.rule_count == 1


def test_replacing_can_switch_case_insensitive_matching():
    firewall = SiteFirewall(denied_paths=("/admin",))
    switched = firewall.replacing({"case_insensitive_paths": True})
    assert switched.case_insensitive_paths
    assert switched.denied_paths == ("/admin",)
    assert not switched.replacing({"denied_paths": []}).denied_paths
    assert switched.replacing({"denied_paths": []}).case_insensitive_paths


def test_a_firewall_survives_a_round_trip_through_its_serialised_form():
    """Persisted policy JSON and the deployment snapshots both take this path."""
    policy = SecurityPolicy(
        under_attack_mode=True,
        firewall={
            "allow_sources": ("198.51.100.0/24",),
            "denied_countries": ("RU",),
            "denied_methods": ("TRACE",),
            "denied_paths": ("/.git",),
            "case_insensitive_paths": True,
        },
    )

    assert SecurityPolicy.model_validate_json(policy.model_dump_json()) == policy


def test_replacing_swaps_the_named_lists_and_keeps_the_rest():
    """What an operator editing one rule at a time means by it.

    An unnamed list is kept; naming one empty clears it.
    """
    firewall = SiteFirewall(
        deny_sources=("203.0.113.0/24",),
        denied_countries=("DE",),
        denied_methods=("TRACE",),
    )

    replaced = firewall.replacing({"deny_sources": ["198.51.100.0/24"]})

    assert replaced.deny_sources == ("198.51.100.0/24",)
    assert replaced.denied_countries == ("DE",)
    assert replaced.denied_methods == ("TRACE",)
    assert firewall.deny_sources == ("203.0.113.0/24",), "the original is unchanged"
    assert firewall.replacing({"denied_countries": []}).denied_countries == ()


def test_replacing_revalidates_what_it_is_given():
    """`model_copy` would not, and every list here reaches an nginx directive."""
    firewall = SiteFirewall(deny_sources=("203.0.113.0/24",))

    with pytest.raises(ValidationError, match="is not an IP address or CIDR"):
        firewall.replacing({"deny_sources": ["not-a-network"]})
    with pytest.raises(ValidationError, match="ISO 3166-1 alpha-2"):
        firewall.replacing({"denied_countries": ["UK"]})
    # Normalisation is the validator's too, so it applies on the way through.
    assert firewall.replacing({"denied_countries": ["de"]}).denied_countries == ("DE",)


def test_replacing_refuses_a_list_a_firewall_does_not_have():
    """A typo that silently changed nothing would read as a rule that applied."""
    with pytest.raises(ValueError, match="no such rule list: deny_contries"):
        SiteFirewall().replacing({"deny_contries": ["DE"]})


def test_allowing_get_also_allows_head():
    """Nginx answers HEAD from the GET handler; refusing it breaks clients."""
    assert SiteFirewall(allowed_methods=("get",)).allowed_methods == ("GET", "HEAD")
    assert SiteFirewall(allowed_methods=("HEAD", "GET")).allowed_methods == (
        "HEAD",
        "GET",
    )
    assert SiteFirewall(allowed_methods=("POST",)).allowed_methods == ("POST",)


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        (
            {"allowed_methods": ("GET",), "denied_methods": ("DELETE",)},
            "cannot both be set",
        ),
        ({"allowed_methods": ("GET IT",)}, "not an HTTP method"),
        ({"allowed_paths": ("api/",)}, "path 'api/' must start with '/'"),
        ({"allowed_paths": ('/a"b',)}, "may not contain whitespace"),
        ({"allowed_paths": ("/api/", "/api/")}, "duplicate path"),
        ({"denied_paths": ("/",)}, "list the paths to serve in allowed_paths"),
        (
            {"allowed_paths": tuple(f"/{i:03d}/" + "x" * 500 for i in range(10))},
            "too long together",
        ),
        (
            # 1,508 characters but 4,511 UTF-8 bytes: Nginx counts bytes.
            {"allowed_paths": tuple("/" + c * 500 for c in "日月火")},
            "too long together",
        ),
    ],
)
def test_an_allowlist_the_edge_could_not_render_is_refused(rules, message):
    with pytest.raises(ValidationError, match=message):
        SiteFirewall(**rules)


def test_denied_paths_carve_out_of_allowed_paths():
    """allow /api/, deny /api/admin: the longer `location ^~` wins at the edge."""
    firewall = SiteFirewall(allowed_paths=("/api/",), denied_paths=("/api/admin",))

    assert firewall.allowed_paths == ("/api/",)
    assert firewall.denied_paths == ("/api/admin",)
    assert not firewall.requires_geoip
    assert SecurityPolicy(firewall=firewall).required_capabilities


def test_a_rate_limit_is_validated_and_normalised_by_the_contract():
    limit = SiteRateLimit(rate=" 10 r/s ", burst=5, paths=(" /login ",))

    assert limit.rate == "10r/s"
    assert limit.zone == "blitzecdn_rate_10rs"
    assert limit.status == 429
    assert limit.paths == ("/login",)
    assert not limit.empty
    assert SiteRateLimit().empty


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        ({"rate": "7r/s"}, "not an available rate"),
        ({"rate": "10r/s", "status": 403}, "429 or 503"),
        ({"rate": "10r/s", "burst": -1}, "greater than or equal to 0"),
        ({"rate": "10r/s", "paths": ("login",)}, "must start with '/'"),
        ({"rate": "10r/s", "paths": ("/a;b",)}, "may not contain whitespace"),
        ({"burst": 10}, "need a rate"),
        ({"paths": ("/login",)}, "need a rate"),
    ],
)
def test_a_rate_limit_the_edge_could_not_render_is_refused(limit, message):
    with pytest.raises(ValidationError, match=message):
        SiteRateLimit(**limit)


def test_a_rate_limit_requests_the_security_capability():
    policy = SecurityPolicy(rate_limit={"rate": "1r/s"})

    assert policy.capability_requirements == {"security": ("rate_limit",)}
    assert SecurityPolicy.model_validate(policy.model_dump()) == policy
