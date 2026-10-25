"""The GeoIP capability: its metadata, and how an installation finds it.

What core keeps — the three country settings, and the derivation that turns
them into a token — is asserted in the control plane's own suite. What is
asserted here is this distribution: the token it answers for, and the fact that
attaching it is the only thing that makes a country-aware site deployable.
"""

from control_plane_fixtures import build_application

from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.composition import (
    BUILTIN_FEATURES,
    load_registry,
)
from blitzecdn.core.features import FeatureMetadata
from blitzecdn.providers.geoip import __version__
from blitzecdn.providers.geoip.feature import FEATURES

(FEATURE,) = FEATURES


def _site(**policy: object) -> CdnSite:
    return CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
        policy=SitePolicy.from_settings({"compression": "off"} | policy),
    )


# --- registration -----------------------------------------------------------


def test_plugin_provides_geoip_as_a_required_provider() -> None:
    metadata = FEATURE.metadata

    assert isinstance(metadata, FeatureMetadata)
    assert metadata.name == "geoip"
    assert metadata.capabilities == frozenset({"geoip"})
    assert metadata.version == __version__


def test_the_capability_token_matches_what_the_site_contract_asks_for() -> None:
    """The two halves of "requested but absent" have to name the same token.

    Core decides the string a country-aware site asks for and this feature
    decides the string an installation answers with. They are in different
    distributions, so nothing but this makes them agree — and a token that
    disagreed would refuse every country site with the package installed.
    """
    requested = _site(visitor_headers={"ip_country": True}).policy.required_capabilities

    assert requested & FEATURE.metadata.capabilities == frozenset({"geoip"})


def test_geoip_is_a_built_in() -> None:
    """One registration path: the entry point in this distribution's metadata.

    Registered both ways it would collide on its own name at startup, and the
    message would blame the entry point rather than the leftover line.
    """
    assert any("geoip" in module for module in BUILTIN_FEATURES)
    builtins = load_registry()
    assert "geoip" in builtins.capabilities


def test_application_assembly_supplies_the_capability() -> None:
    """Discovery over the real installed metadata, as a control plane does it."""
    registry = load_registry()

    assert "geoip" in registry.capabilities
    assert "geoip" in {feature.name for feature in registry.metadata}


# --- what installing it changes for a deployment ----------------------------


def test_an_installed_geoip_capability_clears_the_site_validation() -> None:
    """The mirror of core's "requested but absent" test.

    Same site, same token, opposite answer — which is the whole of what
    attaching this distribution changes for a deployment.
    """
    registry = load_registry()

    for policy in (
        {"visitor_headers": {"ip_country": True}},
        {"firewall": {"allowed_countries": ["DE"]}},
        {"firewall": {"denied_countries": ["RU"]}},
    ):
        assert registry.missing(_site(**policy).policy.required_capabilities) == ()


def test_a_site_that_asks_for_no_country_needs_this_package_for_nothing() -> None:
    """Installed is not enabled: attaching changes no ordinary site.

    A hostname served with source, method and path filtering — every firewall
    rule that is not geographical — requires `security` and nothing here.
    """
    ordinary = _site(
        visitor_headers={"connecting_ip": True},
        firewall={"deny_sources": ["203.0.113.0/24"], "denied_methods": ["TRACE"]},
    )

    assert "geoip" not in ordinary.policy.required_capabilities


def test_the_capability_contributes_no_desired_state(settings) -> None:
    """Attaching converges nothing on its own.

    Whether an edge resolves countries is fleet Ansible policy
    (`blitzecdn_edge_geoip_enabled`), not a variable the control plane derives,
    so the desired-state document is byte-identical with and without this
    distribution installed. A contribution added here later would silently
    override what an operator set in group vars.
    """
    control = build_application(settings=settings)
    sites = (_site(visitor_headers={"ip_country": True}),)

    try:
        assert "geoip" not in str(control.rendering.fleet_variables(sites))
        assert "geoip" not in str(control.rendering.site_variables(sites[0]))
    finally:
        control.close()
