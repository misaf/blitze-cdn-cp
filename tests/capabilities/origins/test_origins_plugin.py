"""The origin-check capability: its metadata, its surface, and its one slot-less
Ansible contribution.
"""

from __future__ import annotations

from blitzecdn.capabilities.origins import __version__, ansible
from blitzecdn.capabilities.origins.feature import FEATURES
from blitzecdn.composition import BUILTIN_FEATURES, load_registry
from blitzecdn.core.plugins import PluginMetadata
from blitzecdn.core.plugins.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
)

(FEATURE,) = FEATURES

# --- registration -----------------------------------------------------------


def test_plugin_provides_origins_as_a_required_provider() -> None:
    metadata = FEATURE.metadata

    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "origins"
    assert metadata.capabilities == frozenset({"origins"})
    assert metadata.version == __version__


def test_origins_is_a_built_in() -> None:
    """One registration path: the composition root's roster."""
    assert any("origins" in module for module in BUILTIN_FEATURES)


def test_the_shipped_registry_includes_it() -> None:
    registry = load_registry()

    assert "origins" in {plugin.name for plugin in registry.plugins}


# --- the surface it contributes ---------------------------------------------


def test_it_contributes_both_api_versions_and_the_origin_command_group() -> None:
    """The command reads `blitzecdn origin check`, exactly as it did in core.

    The command tree is an interface decision, not a consequence of packaging:
    an operator upgrading past the extraction types what they typed before.
    """
    routes = {route.path for router in FEATURE.api_routers for route in router.routes}
    assert routes == {"/v1/origins/check"}

    groups = FEATURE.cli_commands
    assert [group.name for group in groups] == ["origin"]
    assert [command.name for command in groups[0].app.registered_commands] == ["check"]


# --- Ansible ----------------------------------------------------------------


def test_the_role_is_shipped_but_asked_of_neither_slot_of_the_edge_play() -> None:
    """An operation converges nothing, and must not be able to.

    The role is reached only by this package's own play, on demand. A
    contribution that also named a slot would run a probe on every deploy —
    slower, and a deploy that failed because an origin was briefly down.
    """
    contributions = FEATURE.ansible

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.plugin == "origins"
    assert contribution.edge_roles == ()
    assert contribution.host_roles == ()
    assert resolve_edge_capability_roles(contributions) == ()
    assert resolve_host_capability_roles(contributions) == ()


def test_the_wheel_carries_the_role_and_the_play_it_runs() -> None:
    """The paths a deployment resolves by, not directories in the checkout."""
    assert (ansible.ROLES_PATH / "blitzecdn_origins" / "tasks/main.yml").is_file()
    assert ansible.ORIGIN_CHECK_PLAYBOOK.is_file()


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential and no variable in the deployment document.

    Which is what makes attaching and detaching it invisible to every site: the
    document a rollback converges months from now is byte-identical either way.
    """
    # Configuration is its own contribution, and this capability makes none.
    assert FEATURE.configuration == ()
