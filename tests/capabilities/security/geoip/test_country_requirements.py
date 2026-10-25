"""Which configurations need the GeoIP capability, and which deliberately do not.

`SitePolicy.capability_requirements` is generic; these check the exact set of
country settings against the real registry.
"""

import pytest

from blitzecdn.capabilities.security.geoip.feature import FEATURES
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.composition import load_registry

(FEATURE,) = FEATURES

#: Every site setting that asks the edge which country a visitor is in. A new one
#: belongs here and on `capability_requirements`.
COUNTRY_SETTINGS: tuple[tuple[str, dict[str, object]], ...] = (
    ("visitor_headers.ip_country", {"visitor_headers": {"ip_country": True}}),
    ("firewall.allowed_countries", {"firewall": {"allowed_countries": ["DE", "FR"]}}),
    ("firewall.denied_countries", {"firewall": {"denied_countries": ["RU"]}}),
)


def _site(**policy: object) -> CdnSite:
    return CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
        policy=SitePolicy.from_settings(
            {"compression": "off", "cache_enabled": False} | policy
        ),
    )


@pytest.mark.parametrize(("setting", "policy"), COUNTRY_SETTINGS, ids=lambda v: str(v))
def test_every_country_setting_requests_this_capability(
    setting: str, policy: dict[str, object]
) -> None:
    site = _site(**policy)

    assert "geoip" in site.policy.required_capabilities
    assert setting in " ".join(site.policy.capability_requirements["geoip"])


@pytest.mark.parametrize(("_setting", "policy"), COUNTRY_SETTINGS, ids=lambda v: str(v))
def test_every_country_setting_deploys_once_the_capability_is_installed(
    _setting: str, policy: dict[str, object]
) -> None:
    registry = load_registry()
    assert registry.missing(_site(**policy).policy.required_capabilities) == ()


def test_the_country_header_and_the_country_rules_are_one_capability() -> None:
    """Two consumers, one token.

    A site asking for both reports the capability once, with both settings named.
    """
    site = _site(
        visitor_headers={"ip_country": True},
        firewall={"denied_countries": ["RU"]},
    )

    assert site.policy.required_capabilities == frozenset({"geoip", "security"})
    assert len(site.policy.capability_requirements["geoip"]) == 2
    assert FEATURE.metadata.capabilities == frozenset({"geoip"})


def test_country_rules_ask_for_security_as_well_and_geoip_does_not_supply_it() -> None:
    """A firewall country list is two capabilities, and they stay separate.

    `SecurityPolicy` owns the rule and GeoIP the lookup it needs.
    """
    site = _site(firewall={"allowed_countries": ["DE"]})

    assert site.policy.required_capabilities == frozenset({"geoip", "security"})
    assert "security" not in FEATURE.metadata.capabilities


def test_a_disabled_site_asks_for_nothing_whatever_it_is_configured_with() -> None:
    """A disabled site converges no server block, so it needs no capability."""
    site = _site(enabled=False, visitor_headers={"ip_country": True})

    assert site.policy.required_capabilities == frozenset()


def test_the_connecting_ip_header_alone_needs_no_country_lookup() -> None:
    """`BZ-Connecting-IP` is the default and is not geographical.

    The header block is replaced wholesale when patched, so turning the country
    header off must leave a site that needs no GeoIP.
    """
    site = _site(visitor_headers={"connecting_ip": True, "ip_country": False})

    assert site.policy.required_capabilities == frozenset()
