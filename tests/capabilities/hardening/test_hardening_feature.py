"""The hardening capability: its metadata, and which slots it asks for.

Three Ansible roles and nothing else, so the boundary is what is asserted:
optional, requested by no site, and each role in the slot it was written for.
SSH hardening in the edge slot would run before the firewall is validated;
without the teardown slot a decommissioned host stays key-only.
"""

from blitzecdn.capabilities.hardening import __version__, ansible
from blitzecdn.capabilities.hardening.feature import FEATURES
from blitzecdn.composition import BUILTIN_FEATURES, load_registry
from blitzecdn.core.features import FeatureMetadata
from blitzecdn.core.features.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_teardown_capability_roles,
)

(FEATURE,) = FEATURES

# --- registration -----------------------------------------------------------


def test_feature_provides_hardening_as_a_required_provider() -> None:
    metadata = FEATURE.metadata

    assert isinstance(metadata, FeatureMetadata)
    assert metadata.name == "hardening"
    assert metadata.capabilities == frozenset({"hardening"})
    assert metadata.version == __version__


def test_hardening_is_a_built_in() -> None:
    """Hardening is on the built-in roster."""
    assert any("hardening" in module for module in BUILTIN_FEATURES)


def test_the_shipped_registry_includes_it() -> None:
    registry = load_registry()

    assert "hardening" in {feature.name for feature in registry.metadata}


def test_no_site_can_ask_for_this_capability() -> None:
    """Absence is silent here, and that is the whole design.

    Detaching it means something else owns host access, which no site has an
    opinion about. A site field depending on `hardening` would make detaching a
    fleet-wide validation failure.
    """
    registry = load_registry()
    hardening = next(p for p in registry.metadata if p.name == "hardening")

    assert hardening.capabilities == frozenset({"hardening"})
    assert not any(
        "hardening" in site_token
        for feature in registry.metadata
        for site_token in feature.provides
        if feature.name != "hardening"
    )


# --- the slot ---------------------------------------------------------------


def test_the_roles_are_contributed_to_the_host_slot_and_not_the_edge_one() -> None:
    """Where these roles run is the reason the second slot exists.

    In the edge slot, SSH policy would run before `blitzecdn_firewall` is
    validated, risking a key-only host unreachable from the management network.
    """
    contributions = FEATURE.ansible

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.feature == "hardening"
    assert contribution.edge_roles == ()
    assert contribution.host_roles == (
        "blitzecdn_hardening_sshd",
        "blitzecdn_hardening_fail2ban",
    )
    assert resolve_edge_capability_roles(contributions) == ()
    assert resolve_host_capability_roles(contributions) == contribution.host_roles


def test_fail2ban_follows_ssh_within_the_contribution() -> None:
    """Order inside one slot is this capability's, because it owns both roles.

    The jail protects a daemon that has already stopped accepting passwords. A
    contribution's list is kept as declared.
    """
    resolved = resolve_host_capability_roles(FEATURE.ansible)

    assert resolved.index("blitzecdn_hardening_sshd") < resolved.index(
        "blitzecdn_hardening_fail2ban"
    )


def test_the_capability_withdraws_its_own_files_in_the_teardown_slot() -> None:
    """The SSH drop-in and Fail2Ban jail are in no tree `blitzecdn_edge_teardown`
    removes, so this capability withdraws them itself.
    """
    contributions = FEATURE.ansible

    assert contributions[0].teardown_roles == ("blitzecdn_hardening_teardown",)
    assert resolve_teardown_capability_roles(contributions) == (
        "blitzecdn_hardening_teardown",
    )


def test_converging_and_withdrawing_are_different_roles() -> None:
    """A role in both slots would run twice on a decommission.

    It would converge the policy the same run is removing.
    """
    contribution = FEATURE.ansible[0]

    assert not set(contribution.host_roles) & set(contribution.teardown_roles)


def test_the_package_actually_carries_the_roles_it_names() -> None:
    """The path a deployment resolves by, not a directory in the checkout."""
    for role in ansible.HOST_ROLES + ansible.TEARDOWN_ROLES:
        assert (ansible.ROLES_PATH / role / "tasks/main.yml").is_file(), role


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential, no fleet secret, no variable in the deployment document.

    So attaching or detaching it leaves every site's desired state identical.
    """
    # Configuration is its own contribution, and this capability makes none.
    assert FEATURE.configuration == ()
