"""The origin-check capability: its metadata, its surface, and its one slot-less
Ansible contribution.
"""

from __future__ import annotations

from blitzecdn_origins import __version__, ansible
from blitzecdn_origins.plugin import (
    blitzecdn_ansible_contributions,
    blitzecdn_api_routers,
    blitzecdn_cli_commands,
    blitzecdn_plugin_metadata,
)

from blitzecdn.core.plugins import BUILTIN_PLUGINS, PluginMetadata, load_plugins
from blitzecdn.core.plugins.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
)

# --- registration -----------------------------------------------------------


def test_plugin_provides_origins_as_an_optional_capability() -> None:
    metadata = blitzecdn_plugin_metadata()

    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "origins"
    assert metadata.capabilities == frozenset({"origins"})
    assert not metadata.required
    assert metadata.version == __version__


def test_origins_is_never_a_built_in() -> None:
    """One registration path: the entry point in this distribution's metadata."""
    assert not any("origins" in module for module in BUILTIN_PLUGINS)


def test_an_installed_controller_discovers_it_through_its_entry_point() -> None:
    registry = load_plugins()

    assert "origins" in {plugin.name for plugin in registry.plugins}


# --- the surface it contributes ---------------------------------------------


def test_it_contributes_both_api_versions_and_the_origin_command_group() -> None:
    """The command reads `blitzecdn origin check`, exactly as it did in core.

    The command tree is an interface decision, not a consequence of packaging:
    an operator upgrading past the extraction types what they typed before.
    """
    routes = {
        route.path  # type: ignore[attr-defined]
        for router in blitzecdn_api_routers()
        for route in router.routes
    }
    assert routes == {"/v1/origins/check"}

    groups = blitzecdn_cli_commands()
    assert [group.name for group in groups] == ["origin"]
    assert [command.name for command in groups[0].app.registered_commands] == ["check"]


# --- Ansible ----------------------------------------------------------------


def test_the_role_is_shipped_but_asked_of_neither_slot_of_the_edge_play() -> None:
    """An operation converges nothing, and must not be able to.

    The role is reached only by this package's own play, on demand. A
    contribution that also named a slot would run a probe on every deploy —
    slower, and a deploy that failed because an origin was briefly down.
    """
    contributions = blitzecdn_ansible_contributions()

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
    assert blitzecdn_ansible_contributions()[0].environment_keys == ()
