"""The resolver capability: its metadata, and the two slots it asks for.

A narrow optional distribution — two Ansible roles and nothing else — so what
is worth asserting is exactly the boundary: that it registers as optional, that
no site can request it, and that its roles land in the two slots that matter,
one per play. The second of those is the one this package exists to prove:
a capability that puts a file on a host must be able to take it off again
without core knowing the path.
"""

from importlib import import_module

from blitzecdn_resolver import __version__, ansible
from blitzecdn_resolver.plugin import (
    blitzecdn_ansible_contributions,
    blitzecdn_plugin_metadata,
)

from blitzecdn.core.plugins import BUILTIN_PLUGINS, PluginMetadata, load_plugins
from blitzecdn.core.plugins.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_teardown_capability_roles,
)

# --- registration -----------------------------------------------------------


def test_plugin_provides_resolver_as_an_optional_capability() -> None:
    metadata = blitzecdn_plugin_metadata()

    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "resolver"
    assert metadata.capabilities == frozenset({"resolver"})
    assert not metadata.required
    assert metadata.version == __version__


def test_resolver_is_never_a_built_in() -> None:
    """One registration path: the entry point in this distribution's metadata.

    Registered both ways it would collide on its own name at startup, and the
    message would blame the entry point rather than the leftover line.
    """
    assert not any("resolver" in module for module in BUILTIN_PLUGINS)


def test_an_installed_controller_discovers_it_through_its_entry_point() -> None:
    registry = load_plugins()

    assert "resolver" in {plugin.name for plugin in registry.plugins}


def test_no_site_can_ask_for_this_capability() -> None:
    """Absence is silent here, and that is the whole design.

    Several optional capabilities have a site setting that makes their absence
    fatal — a country header, a compression mode, a QUIC listener. This one has
    none: an operator who detaches it is saying "the network owns this host's
    DNS", which no site's desired state has an opinion about. A future field
    that made a site depend on `resolver` would turn detaching from a supported
    choice into a fleet-wide validation failure.
    """
    registry = load_plugins()
    resolver = next(p for p in registry.plugins if p.name == "resolver")

    assert resolver.capabilities == frozenset({"resolver"})


# --- the slots --------------------------------------------------------------


def test_converging_goes_in_the_edge_slot_and_never_the_host_one() -> None:
    """Position is the contract, and this package asks for the earlier one.

    Everything that resolves a name after this role runs depends on its answer
    — most immediately the origin hostnames `blitzecdn_nginx` renders into a
    configuration the runtime will look up. The host slot is on the far side of
    `nginx -t` and would be too late.
    """
    contributions = blitzecdn_ansible_contributions()

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.plugin == "resolver"
    assert contribution.edge_roles == ("blitzecdn_resolver",)
    assert contribution.host_roles == ()
    assert resolve_edge_capability_roles(contributions) == ("blitzecdn_resolver",)
    assert resolve_host_capability_roles(contributions) == ()


def test_withdrawing_goes_in_the_decommission_slot() -> None:
    """The other half, and the reason the third slot exists at all.

    The drop-in this capability writes is at a path only this wheel knows.
    Core's `blitzecdn_teardown` removes the trees it wrote, the shared runtime
    directories and every systemd unit matching the managed prefix — a file
    under /etc/systemd/resolved.conf.d is none of those. Without this
    contribution a decommissioned host would keep resolving through servers
    chosen by a control plane that has forgotten it exists.
    """
    contributions = blitzecdn_ansible_contributions()

    assert contributions[0].teardown_roles == ("blitzecdn_resolver_teardown",)
    assert resolve_teardown_capability_roles(contributions) == (
        "blitzecdn_resolver_teardown",
    )


def test_the_two_slots_name_different_roles() -> None:
    """Converging and withdrawing are separate roles, not one role with a flag.

    A single role guarded by a variable would have to be included by both
    plays, which means the decommission play would run every assertion the
    converge path makes — on a host that is being taken apart, where a resolver
    probe failing is not a reason to abandon a teardown half-way through.
    """
    contribution = blitzecdn_ansible_contributions()[0]

    assert not set(contribution.edge_roles) & set(contribution.teardown_roles)


def test_the_wheel_actually_carries_the_roles_it_names() -> None:
    """The path a deployment resolves by, not a directory in the checkout."""
    for role in (*ansible.EDGE_ROLES, *ansible.TEARDOWN_ROLES):
        assert (ansible.ROLES_PATH / role / "tasks/main.yml").is_file(), role


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential, no fleet secret, no variable in the deployment document.

    Which is what makes attaching and detaching it invisible to every site: the
    document a rollback converges months from now is byte-identical either way.
    """
    # Configuration is its own contribution now, and this capability makes
    # none at all: not implementing the hook is how a package says so.
    assert not hasattr(
        import_module("blitzecdn_resolver.plugin"), "blitzecdn_capability_configuration"
    )
