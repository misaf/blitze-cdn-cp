"""The HTTP/3 capability: its metadata, and the fleet state it derives.

These are the tests that moved out of `tests/capabilities/http/` with the code. The
derivation is asserted here because it is this distribution's behavior; what
core keeps — the switch, the baseline listener stance — is asserted there.
"""

import pytest
from blitzecdn_http3 import __version__
from blitzecdn_http3.plugin import (
    QUIC_FLEET_VARIABLES,
    blitzecdn_fleet_desired_state,
    blitzecdn_plugin_metadata,
)

from blitzecdn.capabilities.http.plugin import (
    blitzecdn_fleet_desired_state as baseline_fleet_desired_state,
)
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.capabilities.tls.policy import managed_certificate_paths
from blitzecdn.core.plugins import PluginMetadata, load_plugins, merge_variables


def _site(name: str, *, enabled: bool = True, http3: bool = True) -> CdnSite:
    chain, key = managed_certificate_paths(name)
    return CdnSite(
        name=name,
        server_names=(f"{name}.example.com",),
        origin_host="198.51.100.10",
        enabled=enabled,
        ssl_mode="full",
        certificate_mode="requested",
        certificate_path=chain,
        certificate_key_path=key,
        http3_enabled=http3,
    )


def _merged(*sites: CdnSite) -> dict[str, object]:
    """The fleet document as a real deployment would merge it.

    Both plugins, through the same `merge_variables` the registry uses — which
    is the only place the `overrides` claim is actually honoured. Asserting on
    this plugin's contribution alone would pass even if the two collided.
    """
    return merge_variables(
        [
            baseline_fleet_desired_state(sites, object()),
            blitzecdn_fleet_desired_state(sites, object()),
        ],
        subject="fleet desired state",
    )


# --- registration -----------------------------------------------------------


def test_plugin_provides_http3_as_an_optional_capability() -> None:
    metadata = blitzecdn_plugin_metadata()

    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "http3"
    assert metadata.capabilities == frozenset({"http3"})
    assert not metadata.required
    assert metadata.version == __version__


def test_the_capability_token_matches_what_the_site_contract_asks_for() -> None:
    """The two halves of "requested but absent" have to name the same token.

    `ProtocolPolicy` decides the string a site asks for and this plugin decides
    the string an installation answers with. They are in different
    distributions, so nothing but this makes them agree.
    """
    requested = _site("alpha").required_capabilities

    assert "http3" in requested
    assert requested & blitzecdn_plugin_metadata().capabilities == frozenset({"http3"})


# --- the fleet derivation ---------------------------------------------------


def test_http3_projects_the_quic_fleet_requirement_and_exactly_one_owner() -> None:
    contribution = blitzecdn_fleet_desired_state(
        (_site("bravo"), _site("alpha"), _site("disabled", enabled=False)), object()
    )

    assert contribution.plugin == "http3"
    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": True,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }


def test_a_fleet_with_no_http3_site_opens_no_quic_listener() -> None:
    contribution = blitzecdn_fleet_desired_state(
        (_site("alpha", http3=False),), object()
    )

    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


def test_a_disabled_site_neither_owns_the_listener_nor_opens_it() -> None:
    """A disabled site converges no server block, so it cannot carry reuseport."""
    contribution = blitzecdn_fleet_desired_state(
        (_site("alpha", enabled=False),), object()
    )

    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


def test_the_owner_is_the_first_site_by_name_whatever_order_they_arrive_in() -> None:
    """Deterministic, so two runs of one fleet render a byte-identical document."""
    sites = (_site("charlie"), _site("alpha"), _site("bravo"))
    forward = blitzecdn_fleet_desired_state(sites, object())
    reversed_ = blitzecdn_fleet_desired_state(tuple(reversed(sites)), object())

    assert forward.variables == reversed_.variables
    assert forward.variables["blitzecdn_nginx_http3_listener_owner"] == "alpha"


# --- merging against the baseline core writes -------------------------------


def test_installed_http3_overrides_the_baseline_core_contributes() -> None:
    """The whole difference installing this package makes to a deployment."""
    assert blitzecdn_fleet_desired_state((), object()).overrides == QUIC_FLEET_VARIABLES
    assert _merged(_site("bravo"), _site("alpha")) == {
        "blitzecdn_edge_http3_enabled": True,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }


def test_the_two_plugins_agree_when_no_site_asks_for_http3() -> None:
    """Attached-but-unused converges exactly what detached converges.

    So attaching the distribution to a fleet that has not turned HTTP/3 on
    anywhere is a no-op at the edge, not a listener that opens because a
    package was installed.
    """
    sites = (_site("alpha", http3=False),)

    assert _merged(*sites) == baseline_fleet_desired_state(sites, object()).variables


def test_the_two_plugins_never_collide_on_the_variables_they_share() -> None:
    """Without the `overrides` claim this raises, which is the point of it."""
    document = _merged(_site("alpha"))

    assert set(document) == QUIC_FLEET_VARIABLES


def test_claiming_the_override_twice_is_refused() -> None:
    """Two plugins deriving the QUIC listener is a conflict, not a last-wins."""
    contribution = blitzecdn_fleet_desired_state((_site("alpha"),), object())

    with pytest.raises(Exception, match="each claim to override"):
        merge_variables([contribution, contribution], subject="fleet desired state")


# --- through the real registry ----------------------------------------------


def test_the_installed_entry_point_derives_the_fleet_state_end_to_end() -> None:
    """Discovery, registration and the merge, in one pass over real metadata.

    `load_plugins` reads this distribution's installed `blitzecdn.plugins`
    entry point exactly as a running control plane does, and `fleet_variables`
    is the call the deployment renderer makes. So this fails if the entry point
    is misspelled, if the hook is not picked up, or if the `overrides` claim
    stops resolving against core's baseline — none of which the unit tests
    above would notice.
    """
    registry = load_plugins()

    assert "http3" in registry.capabilities
    assert registry.fleet_variables((_site("bravo"), _site("alpha")), object()) == {
        "blitzecdn_edge_http3_enabled": True,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }
    assert registry.fleet_variables((_site("alpha", http3=False),), object()) == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


def test_an_installed_http3_capability_clears_the_site_validation() -> None:
    """The mirror of core's "requested but absent" test.

    Same site, same token, opposite answer — which is the whole of what
    attaching this distribution changes for a deployment.
    """
    registry = load_plugins()

    assert registry.missing(_site("alpha").required_capabilities) == ()
