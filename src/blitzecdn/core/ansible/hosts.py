"""Resolving a host limit against the fleet the control plane records."""

from __future__ import annotations

from fnmatch import fnmatch

from blitzecdn.capabilities.edges.domain import EDGE_GROUP
from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.validation import validate_edge_limit

__all__ = ["resolve_limit", "targeted_hosts"]


def resolve_limit(edges: EdgeStore, host_limit: str | None) -> str:
    """Resolve a host limit to explicit edge names, or the whole group.

    The limit is expanded here against the recorded fleet rather than
    handed to Ansible as a pattern. Ansible's own syntax cannot express "this group,
    restricted to any of these names": ``:`` and ``,`` are both union
    separators and ``&`` binds only to the term beside it, so
    ``blitzecdn_edges:&a,b`` means "(edges also matching a) or b" and would
    happily reach a host outside the group. Expanding to a literal list of
    names taken *from* the group removes the question — a limit cannot name
    a host the control plane does not already manage as an edge.

    The second benefit is diagnostic: a typo fails here, naming the edges
    that do exist, instead of becoming Ansible's "skipping: no hosts
    matched" and a deploy that reports success having converged nothing.
    """
    validated = validate_edge_limit(host_limit)
    if validated is None:
        return EDGE_GROUP
    known = [edge.name for edge in edges.list_edges()]
    matched = [
        name
        for name in known
        if any(fnmatch(name, pattern) for pattern in validated.split(","))
    ]
    if not matched:
        raise ConfigurationError(
            f"host limit {validated!r} matches none of the configured edges: "
            + (
                ", ".join(known)
                or "no edges are registered; add one with 'blitzecdn edge add'"
            )
        )
    return ",".join(matched)


def targeted_hosts(edges: EdgeStore, resolved: str) -> tuple[str, ...]:
    """The edges a resolved limit names, for the run record.

    Derived from what :func:`resolve_limit` already produced, and therefore
    from the same rows Ansible is about to be given, so "what did this run aim
    at" cannot disagree with what it actually targeted.
    """
    if resolved == EDGE_GROUP:
        return tuple(edge.name for edge in edges.list_edges())
    return tuple(resolved.split(","))
