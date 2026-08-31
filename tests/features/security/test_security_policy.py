"""The security capability: firewall rules and Under Attack Mode as one owner."""

from dataclasses import dataclass

import pytest
from pydantic import SecretStr, ValidationError

from blitzecdn.features.security import plugin
from blitzecdn.features.security.policy import SecurityPolicy, SiteFirewall
from blitzecdn.features.sites.domain import CdnSite


def _site(*, under_attack: bool, enabled: bool = True) -> CdnSite:
    return CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
        enabled=enabled,
        under_attack_mode=under_attack,
    )


@dataclass(frozen=True)
class _Settings:
    under_attack_secret: SecretStr


@dataclass(frozen=True)
class _Platform:
    settings: _Settings


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


def test_under_attack_without_a_controller_secret_is_a_blocking_issue():
    """The deploy could only fail on every edge, so it is refused before one starts."""
    platform = _Platform(_Settings(SecretStr("")))

    (issue,) = plugin.blitzecdn_deployment_checks(_site(under_attack=True), platform)

    assert issue.plugin == "security"
    assert issue.site == "alpha"
    assert issue.severity == "blocking"
    assert "BLITZE_UNDER_ATTACK_SECRET" in issue.message


def test_no_issue_when_the_secret_is_present_or_the_site_does_not_ask():
    """The three shapes that must stay deployable, beside the one that must not.

    A provisioned controller, a site that never asked, and a disabled site that
    asked but converges no server block.
    """
    provisioned = _Platform(_Settings(SecretStr("x" * 32)))
    missing = _Platform(_Settings(SecretStr("")))

    checks = plugin.blitzecdn_deployment_checks

    assert checks(_site(under_attack=True), provisioned) == ()
    assert checks(_site(under_attack=False), missing) == ()
    assert checks(_site(under_attack=True, enabled=False), missing) == ()
