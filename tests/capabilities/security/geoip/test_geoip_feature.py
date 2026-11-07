"""The GeoIP capability: its metadata, and how an installation finds it.

The country settings and their token derivation are core's. Asserted here: the
token this feature answers for, and that it makes a country-aware site
deployable.
"""

from support.control_plane import build_application

from blitzecdn.capabilities.security.geoip import __version__
from blitzecdn.capabilities.security.geoip.feature import FEATURES
from blitzecdn.composition import (
    BUILTIN_FEATURES,
    load_registry,
)
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.features import FeatureMetadata

(FEATURE,) = FEATURES


def _site(**policy: object) -> CdnSite:
    return CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
        policy=SitePolicy.from_settings({"compression": "off"} | policy),
    )


# --- registration -----------------------------------------------------------


def test_feature_provides_geoip_as_a_required_provider() -> None:
    metadata = FEATURE.metadata

    assert isinstance(metadata, FeatureMetadata)
    assert metadata.name == "geoip"
    assert metadata.capabilities == frozenset({"geoip"})
    assert metadata.version == __version__


def test_the_capability_token_matches_what_the_site_contract_asks_for() -> None:
    """The two halves of "requested but absent" have to name the same token.

    Core decides the token a country-aware site asks for and this feature the one
    it answers with; only this makes them agree.
    """
    requested = _site(visitor_headers={"ip_country": True}).policy.required_capabilities

    assert requested & FEATURE.metadata.capabilities == frozenset({"geoip"})


def test_geoip_is_a_built_in() -> None:
    """GeoIP is on the built-in roster."""
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
    """The mirror of core's "requested but absent" test: same site, same token,
    opposite answer.
    """
    registry = load_registry()

    for policy in (
        {"visitor_headers": {"ip_country": True}},
        {"firewall": {"allowed_countries": ["DE"]}},
        {"firewall": {"denied_countries": ["RU"]}},
    ):
        assert registry.missing(_site(**policy).policy.required_capabilities) == ()


def test_a_site_that_asks_for_no_country_needs_geoip_for_nothing() -> None:
    """Installed is not enabled: it changes no ordinary site.

    Every non-geographical firewall rule requires `security` and nothing here.
    """
    ordinary = _site(
        visitor_headers={"connecting_ip": True},
        firewall={"deny_sources": ["203.0.113.0/24"], "denied_methods": ["TRACE"]},
    )

    assert "geoip" not in ordinary.policy.required_capabilities


def test_the_capability_contributes_no_desired_state_without_a_database(
    settings,
) -> None:
    """Attaching converges nothing on its own.

    Whether an edge resolves countries is fleet Ansible policy
    (`blitzecdn_geoip_enabled`); until the controller holds a database there is
    nothing to pin either.
    """
    control = build_application(settings=settings)
    sites = (_site(visitor_headers={"ip_country": True}),)

    try:
        assert "geoip" not in str(control.rendering.fleet_variables(sites))
        assert "geoip" not in str(control.rendering.site_variables(sites[0]))
    finally:
        control.close()


def test_a_refreshed_database_reaches_the_fleet_with_the_next_deployment(
    settings,
) -> None:
    """The requirement is owed until a forward fleet run ships the new file."""
    from support.control_plane import FakeRunner, seed_site

    from blitzecdn.capabilities.deployments.domain import (
        DeploymentRequirementKind,
        DeploymentStatus,
    )
    from blitzecdn.capabilities.security.geoip.domain import (
        DATABASE_SHA256_VARIABLE,
        DATABASE_SOURCE_VARIABLE,
    )
    from blitzecdn.capabilities.security.geoip.ports import DatabaseStore
    from blitzecdn.composition import Repository

    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control)
    kind = DeploymentRequirementKind.GEOIP_DATABASE
    requirements = repository.deployment_requirements
    try:
        store = control.container.get(DatabaseStore)
        installed = store.install(b"\0" * 64 + b"\xab\xcd\xefMaxMind.com")
        requirements.require(kind)

        queued = control.deployments.converge_requirements("scheduler")
        assert queued is not None
        assert control.deployments.converge(queued, "scheduler").status is (
            DeploymentStatus.SUCCEEDED
        )
        assert not requirements.pending(kind)

        fleet = control.rendering.fleet_variables(())
        assert fleet[DATABASE_SOURCE_VARIABLE] == str(installed.path)
        assert fleet[DATABASE_SHA256_VARIABLE] == installed.sha256
    finally:
        control.close()
