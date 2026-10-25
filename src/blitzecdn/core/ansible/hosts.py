"""Resolving a host limit against the fleet the control plane records."""

from __future__ import annotations

from blitzecdn.core.domain.validation import matches_edge_limit, validate_edge_limit
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.ports.fleet import FleetRoster

__all__ = ["resolve_limit", "targeted_hosts"]


def resolve_limit(fleet: FleetRoster, host_limit: str | None) -> str:
    """Resolve a host limit to explicit edge names, or the whole group.

    Expanded here rather than passed as an Ansible pattern, which cannot express
    "this group, restricted to these names" — ``blitzecdn_edges:&a,b`` can reach a
    host outside the group. A literal list taken from the group cannot, and a typo
    fails here naming the real edges instead of matching nothing.
    """
    validated = validate_edge_limit(host_limit)
    if validated is None:
        return fleet.group
    known = list(fleet.host_names())
    matched = [name for name in known if matches_edge_limit(name, validated)]
    if not matched:
        raise ConfigurationError(
            f"host limit {validated!r} matches none of the configured edges: "
            + (
                ", ".join(known)
                or "no edges are registered; add one with 'blitzecdn edge add'"
            )
        )
    return ",".join(matched)


def targeted_hosts(fleet: FleetRoster, resolved: str) -> tuple[str, ...]:
    """The edges a resolved limit names, derived from what Ansible will be given."""
    if resolved == fleet.group:
        return fleet.host_names()
    return tuple(resolved.split(","))
