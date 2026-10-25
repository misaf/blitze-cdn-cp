"""The resolver capability: its metadata, and the two slots it asks for.

Two Ansible roles and nothing else, so the boundary is what is asserted:
optional, requested by no site, and its roles in one slot per play. A
capability that puts a file on a host must take it off without core knowing
the path.
"""

from blitzecdn.capabilities.resolver import __version__, ansible
from blitzecdn.capabilities.resolver.feature import FEATURES
from blitzecdn.composition import BUILTIN_FEATURES, load_registry
from blitzecdn.core.features import FeatureMetadata
from blitzecdn.core.features.resolution import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_teardown_capability_roles,
)

(FEATURE,) = FEATURES

# --- registration -----------------------------------------------------------


def test_plugin_provides_resolver_as_a_required_provider() -> None:
    metadata = FEATURE.metadata

    assert isinstance(metadata, FeatureMetadata)
    assert metadata.name == "resolver"
    assert metadata.capabilities == frozenset({"resolver"})
    assert metadata.version == __version__


def test_resolver_is_a_built_in() -> None:
    """The resolver is on the built-in roster."""
    assert any("resolver" in module for module in BUILTIN_FEATURES)


def test_the_shipped_registry_includes_it() -> None:
    registry = load_registry()

    assert "resolver" in {feature.name for feature in registry.metadata}


def test_no_site_can_ask_for_this_capability() -> None:
    """Absence is silent here, and that is the whole design.

    Detaching it means the network owns this host's DNS, which no site has an
    opinion about.
    """
    registry = load_registry()
    resolver = next(p for p in registry.metadata if p.name == "resolver")

    assert resolver.capabilities == frozenset({"resolver"})


# --- the slots --------------------------------------------------------------


def test_converging_goes_in_the_edge_slot_and_never_the_host_one() -> None:
    """Position is the contract, and this capability asks for the earlier one.

    Origin hostnames `blitzecdn_nginx` renders depend on its answer; the host slot
    comes after `nginx -t`, too late.
    """
    contributions = FEATURE.ansible

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.feature == "resolver"
    assert contribution.edge_roles == ("blitzecdn_resolver",)
    assert contribution.host_roles == ()
    assert resolve_edge_capability_roles(contributions) == ("blitzecdn_resolver",)
    assert resolve_host_capability_roles(contributions) == ()


def test_withdrawing_goes_in_the_decommission_slot() -> None:
    """The other half, and the reason the third slot exists at all.

    A drop-in under /etc/systemd/resolved.conf.d is in nothing
    `blitzecdn_edge_teardown` removes; without this a decommissioned host keeps
    BlitzeCDN's resolvers.
    """
    contributions = FEATURE.ansible

    assert contributions[0].teardown_roles == ("blitzecdn_resolver_teardown",)
    assert resolve_teardown_capability_roles(contributions) == (
        "blitzecdn_resolver_teardown",
    )


def test_the_two_slots_name_different_roles() -> None:
    """Converging and withdrawing are separate roles, not one role with a flag.

    A shared role would run the converge path's assertions during a teardown,
    where a failed resolver probe must not abandon it half-way.
    """
    contribution = FEATURE.ansible[0]

    assert not set(contribution.edge_roles) & set(contribution.teardown_roles)


def test_the_wheel_actually_carries_the_roles_it_names() -> None:
    """The path a deployment resolves by, not a directory in the checkout."""
    for role in (*ansible.EDGE_ROLES, *ansible.TEARDOWN_ROLES):
        assert (ansible.ROLES_PATH / role / "tasks/main.yml").is_file(), role


def test_the_capability_claims_no_environment_and_no_desired_state() -> None:
    """No credential, no fleet secret, no variable in the deployment document.

    So attaching or detaching it leaves every site's desired state identical.
    """
    # Configuration is its own contribution, and this capability makes none.
    assert FEATURE.configuration == ()
