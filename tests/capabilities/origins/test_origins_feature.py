"""The origin-check capability: its metadata, its surface, and its one slot-less
Ansible contribution.
"""

from __future__ import annotations

from blitzecdn.capabilities.origins import __version__, ansible
from blitzecdn.capabilities.origins.feature import FEATURES
from blitzecdn.composition import BUILTIN_FEATURES, load_registry
from blitzecdn.core.features import FeatureMetadata
from blitzecdn.core.features.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
)

(FEATURE,) = FEATURES

# --- registration -----------------------------------------------------------


def test_feature_provides_origins_as_a_required_provider() -> None:
    metadata = FEATURE.metadata

    assert isinstance(metadata, FeatureMetadata)
    assert metadata.name == "origins"
    assert metadata.capabilities == frozenset({"origins"})
    assert metadata.version == __version__


def test_origins_is_a_built_in() -> None:
    """One registration path: the composition root's roster."""
    assert any("origins" in module for module in BUILTIN_FEATURES)


def test_the_shipped_registry_includes_it() -> None:
    registry = load_registry()

    assert "origins" in {feature.name for feature in registry.metadata}


# --- the surface it contributes ---------------------------------------------


def test_it_contributes_both_api_versions_and_the_origin_command_group() -> None:
    """The command reads `blitzecdn origin check`."""
    routes = {route.path for router in FEATURE.api_routers for route in router.routes}
    assert routes == {"/v1/origins/check"}

    groups = FEATURE.cli_commands
    assert [group.name for group in groups] == ["origin"]
    assert [command.name for command in groups[0].app.registered_commands] == ["check"]


# --- Ansible ----------------------------------------------------------------


def test_the_role_is_shipped_but_asked_of_neither_slot_of_the_edge_play() -> None:
    """An operation converges nothing, and must not be able to.

    The role runs only from this capability's own play; in a slot it would probe
    on every deploy.
    """
    contributions = FEATURE.ansible

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.feature == "origins"
    assert contribution.edge_roles == ()
    assert contribution.host_roles == ()
    assert resolve_edge_capability_roles(contributions) == ()
    assert resolve_host_capability_roles(contributions) == ()


def test_the_package_carries_the_role_and_the_play_it_runs() -> None:
    """The paths a deployment resolves by, not directories in the checkout."""
    assert (ansible.ROLES_PATH / "blitzecdn_origins" / "tasks/main.yml").is_file()
    assert ansible.ORIGIN_CHECK_PLAYBOOK.is_file()


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential and no variable in the deployment document.

    So attaching or detaching it leaves every site's desired state identical.
    """
    # Configuration is its own contribution, and this capability makes none.
    assert FEATURE.configuration == ()
