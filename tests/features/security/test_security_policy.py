"""The security capability: firewall rules and Under Attack Mode as one owner."""

import pytest
from pydantic import ValidationError

from blitzecdn.features.security.policy import SecurityPolicy, SiteFirewall


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
