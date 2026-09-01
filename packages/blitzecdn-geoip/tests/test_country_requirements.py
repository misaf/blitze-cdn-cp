"""Which configurations need this package, and which deliberately do not.

The rules live in core — `SitePolicy.capability_requirements` is generic and
knows nothing about this distribution — so these are the *capability's* side of
them: the exact set of settings whose absence-behavior this wheel is
responsible for, checked against the real registry with it installed.

They are here rather than in `tests/` because they are what an operator asks
about this package. Core's suite asserts the same derivation with nothing
installed, which is the case this file cannot see.
"""

import pytest
from blitzecdn_geoip.plugin import blitzecdn_plugin_metadata

from blitzecdn.core.plugins import load_plugins
from blitzecdn.features.sites.domain import CdnSite

#: Every setting on the stable site schema that asks the edge which country a
#: visitor is in, and nothing else. A fourth consumer belongs on this list and
#: on `capability_requirements` in core — not in a new distribution.
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
        compression="off",
        **policy,
    )


@pytest.mark.parametrize(("setting", "policy"), COUNTRY_SETTINGS, ids=lambda v: str(v))
def test_every_country_setting_requests_this_capability(
    setting: str, policy: dict[str, object]
) -> None:
    site = _site(**policy)

    assert "geoip" in site.required_capabilities
    assert setting in " ".join(site.capability_requirements["geoip"])


@pytest.mark.parametrize(("_setting", "policy"), COUNTRY_SETTINGS, ids=lambda v: str(v))
def test_every_country_setting_deploys_once_the_capability_is_installed(
    _setting: str, policy: dict[str, object]
) -> None:
    assert load_plugins().missing(_site(**policy).required_capabilities) == ()


def test_the_country_header_and_the_country_rules_are_one_capability() -> None:
    """Two consumers, one token — which is why there is one wheel, not two.

    A site asking for both reports the capability once, with both settings
    named, rather than two tokens an operator would have to attach separately.
    """
    site = _site(
        visitor_headers={"ip_country": True},
        firewall={"denied_countries": ["RU"]},
    )

    assert site.required_capabilities == frozenset({"geoip", "security"})
    assert len(site.capability_requirements["geoip"]) == 2
    assert blitzecdn_plugin_metadata().capabilities == frozenset({"geoip"})


def test_country_rules_ask_for_security_as_well_and_geoip_does_not_supply_it() -> None:
    """A firewall country list is two capabilities, and they stay separate.

    `SecurityPolicy` owns the rule and this package owns the lookup it needs.
    A `blitzecdn-geoip` that answered for `security` too would make detaching
    the firewall implementation silently survivable.
    """
    site = _site(firewall={"allowed_countries": ["DE"]})

    assert site.required_capabilities == frozenset({"geoip", "security"})
    assert "security" not in blitzecdn_plugin_metadata().capabilities


def test_a_disabled_site_asks_for_nothing_whatever_it_is_configured_with() -> None:
    """A disabled site converges no server block, so it needs no capability."""
    site = _site(enabled=False, visitor_headers={"ip_country": True})

    assert site.required_capabilities == frozenset()


def test_the_connecting_ip_header_alone_needs_no_country_lookup() -> None:
    """`BZ-Connecting-IP` is the default and is not geographical.

    The header block is replaced wholesale when patched, so turning the country
    header off has to leave the visitor-IP header working with this package
    detached — otherwise the default site would depend on an optional wheel.
    """
    site = _site(visitor_headers={"connecting_ip": True, "ip_country": False})

    assert site.required_capabilities == frozenset()
