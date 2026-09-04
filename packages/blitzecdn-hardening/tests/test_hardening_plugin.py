"""The hardening capability: its metadata, and which slots it asks for.

A narrow optional distribution — three Ansible roles and nothing else — so what
is worth asserting is exactly the boundary: that it registers as optional, that
no site can request it, and that each role it names lands in the slot it was
written for. Neither position is cosmetic. Running SSH hardening in the edge
slot would put it before the firewall has been validated; leaving the withdrawal
out of the teardown slot leaves a decommissioned host public-key-only against a
control plane that has forgotten it exists.
"""

from importlib import import_module

from blitzecdn_hardening import __version__, ansible
from blitzecdn_hardening.plugin import (
    blitzecdn_ansible_contributions,
    blitzecdn_plugin_metadata,
)

from blitzecdn.composition import BUILTIN_PLUGINS, load_control_plane_plugins
from blitzecdn.core.plugins import PluginMetadata
from blitzecdn.core.plugins.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_teardown_capability_roles,
)

# --- registration -----------------------------------------------------------


def test_plugin_provides_hardening_as_an_optional_capability() -> None:
    metadata = blitzecdn_plugin_metadata()

    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "hardening"
    assert metadata.capabilities == frozenset({"hardening"})
    assert not metadata.required
    assert metadata.version == __version__


def test_hardening_is_never_a_built_in() -> None:
    """One registration path: the entry point in this distribution's metadata.

    Registered both ways it would collide on its own name at startup, and the
    message would blame the entry point rather than the leftover line.
    """
    assert not any("hardening" in module for module in BUILTIN_PLUGINS)


def test_an_installed_controller_discovers_it_through_its_entry_point() -> None:
    registry = load_control_plane_plugins()

    assert "hardening" in {plugin.name for plugin in registry.plugins}


def test_no_site_can_ask_for_this_capability() -> None:
    """Absence is silent here, and that is the whole design.

    Every other optional capability has a site setting that makes its absence
    fatal — a country header, a compression mode, a QUIC listener. This one has
    none: an operator who detaches it is saying "something else owns host
    access", which no site's desired state has an opinion about. A future field
    that made a site depend on `hardening` would turn detaching from a
    supported choice into a fleet-wide validation failure.
    """
    registry = load_control_plane_plugins()
    hardening = next(p for p in registry.plugins if p.name == "hardening")

    assert hardening.capabilities == frozenset({"hardening"})
    assert not any(
        "hardening" in site_token
        for plugin in registry.plugins
        for site_token in plugin.provides
        if plugin.name != "hardening"
    )


# --- the slot ---------------------------------------------------------------


def test_the_roles_are_contributed_to_the_host_slot_and_not_the_edge_one() -> None:
    """Where these roles run is the reason the second slot exists.

    SSH policy in the edge slot would run before `blitzecdn_firewall` has been
    validated, which is how a host ends up key-only *and* unreachable from the
    management network. The play enforces the position; this enforces that the
    package asked for that position.
    """
    contributions = blitzecdn_ansible_contributions()

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.plugin == "hardening"
    assert contribution.edge_roles == ()
    assert contribution.host_roles == ("blitzecdn_sshd", "blitzecdn_fail2ban")
    assert resolve_edge_capability_roles(contributions) == ()
    assert resolve_host_capability_roles(contributions) == contribution.host_roles


def test_fail2ban_follows_ssh_within_the_contribution() -> None:
    """Order inside one slot is this package's, because it owns both roles.

    Fail2Ban's jail has to protect a daemon that has already stopped accepting
    passwords. Core orders *between* packages alphabetically and has no way to
    express this; it does not need one, because a single contribution's list is
    kept as declared.
    """
    resolved = resolve_host_capability_roles(blitzecdn_ansible_contributions())

    assert resolved.index("blitzecdn_sshd") < resolved.index("blitzecdn_fail2ban")


def test_the_capability_withdraws_its_own_files_in_the_teardown_slot() -> None:
    """The mirror of the host slot, and the reason core stopped naming these.

    An SSH drop-in and a Fail2Ban jail are in no tree `blitzecdn_teardown`
    removes, and neither is a systemd unit matching the managed prefix. Core
    used to carry both paths in that role's defaults and reload both services
    from its own handlers — a role installed on every controller holding the
    paths of a capability that may not be installed. Declaring the slot here is
    what let those come out.
    """
    contributions = blitzecdn_ansible_contributions()

    assert contributions[0].teardown_roles == ("blitzecdn_hardening_teardown",)
    assert resolve_teardown_capability_roles(contributions) == (
        "blitzecdn_hardening_teardown",
    )


def test_converging_and_withdrawing_are_different_roles() -> None:
    """A role in both slots would run twice on a decommission.

    The host slot is in the edge play and the teardown slot is in the
    decommission play, so a name in both would converge the very policy the
    same run is removing — on a host nothing can reach afterwards.
    """
    contribution = blitzecdn_ansible_contributions()[0]

    assert not set(contribution.host_roles) & set(contribution.teardown_roles)


def test_the_wheel_actually_carries_the_roles_it_names() -> None:
    """The path a deployment resolves by, not a directory in the checkout."""
    for role in ansible.HOST_ROLES + ansible.TEARDOWN_ROLES:
        assert (ansible.ROLES_PATH / role / "tasks/main.yml").is_file(), role


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential, no fleet secret, no variable in the deployment document.

    Which is what makes attaching and detaching it invisible to every site: the
    document a rollback converges months from now is byte-identical either way.
    """
    # Configuration is its own contribution now, and this capability makes
    # none at all: not implementing the hook is how a package says so.
    assert not hasattr(
        import_module("blitzecdn_hardening.plugin"),
        "blitzecdn_capability_configuration",
    )
