"""The security capability: firewall rules and Under Attack Mode as one owner."""

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.security.policy import SecurityPolicy, SiteFirewall


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
    """The validation moved with its vocabulary, so assert it from the outside.

    The country and HTTP-method tables were in `core/validation.py` with this
    as their only consumer. Nothing about the rules changed; what changed is
    that core no longer carries them, and these hold the behaviour to that.
    """
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


def test_a_firewall_survives_a_round_trip_through_its_serialised_form():
    """Persisted policy JSON and the deployment snapshots both take this path."""
    policy = SecurityPolicy(
        under_attack_mode=True,
        firewall={
            "allow_sources": ("198.51.100.0/24",),
            "denied_countries": ("RU",),
            "denied_methods": ("TRACE",),
            "denied_paths": ("/.git",),
        },
    )

    assert SecurityPolicy.model_validate_json(policy.model_dump_json()) == policy
