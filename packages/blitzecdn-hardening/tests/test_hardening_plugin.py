"""The hardening capability: its metadata, and which slot it asks for.

The narrowest optional distribution in the workspace — two Ansible roles and
nothing else — so what is worth asserting is exactly the boundary: that it
registers as optional, that no site can request it, and that the roles it names
land in the play's *host* slot rather than the edge one. The last is not
cosmetic; running SSH hardening in the edge slot would put it before the
firewall has been validated.
"""

from blitzecdn_hardening import __version__, ansible
from blitzecdn_hardening.plugin import (
    blitzecdn_ansible_contributions,
    blitzecdn_plugin_metadata,
)

from blitzecdn.core.ansible.roles import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
)
from blitzecdn.core.plugins import BUILTIN_PLUGINS, PluginMetadata, load_plugins

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
    registry = load_plugins()

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
    registry = load_plugins()
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


def test_the_wheel_actually_carries_the_roles_it_names() -> None:
    """The path a deployment resolves by, not a directory in the checkout."""
    for role in ansible.HOST_ROLES:
        assert (ansible.ROLES_PATH / role / "tasks/main.yml").is_file(), role


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential, no fleet secret, no variable in the deployment document.

    Which is what makes attaching and detaching it invisible to every site: the
    document a rollback converges months from now is byte-identical either way.
    """
    contribution = blitzecdn_ansible_contributions()[0]

    assert contribution.environment_keys == ()
