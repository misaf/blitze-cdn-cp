"""Where a role is, and which roles core's own plays run in each slot.

The search path answers where a role is; the slot resolvers answer which roles
a play includes. A feature whose roles only its own plays reach declares a
directory and no slot.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import AnsibleContribution

__all__ = [
    "resolve_edge_capability_roles",
    "resolve_host_capability_roles",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]


#: How core is named in a conflict message. Not a feature name — nothing
#: registers under it — only the label the failure needs to name both sides.
_CORE = "the control plane"


def resolve_role_search_path(
    core_roles: Path, contributions: Iterable[AnsibleContribution]
) -> tuple[Path, ...]:
    """Core's roles directory, then each feature's, ordered by feature name.

    A role name in two directories is refused with both owners named: Ansible
    takes the first match, so a feature shipping ``blitzecdn_nginx`` would
    silently replace the edge renderer. A declared directory that is missing is
    refused here rather than as an unexplained "role not found" mid-play.
    """
    ordered = sorted(contributions, key=lambda item: item.feature)
    search: list[Path] = [core_roles]
    owners: dict[str, str] = dict.fromkeys(_role_names(core_roles), _CORE)
    for contribution in ordered:
        path = contribution.roles_path
        if not path.is_dir():
            raise FeatureError(
                f"feature {contribution.feature!r} contributes the Ansible roles "
                f"directory {path}, which does not exist. The package was "
                "built without its Ansible resources, or was installed in a way "
                "that did not carry them."
            )
        for role in _role_names(path):
            if role in owners:
                raise FeatureError(
                    f"role {role!r} is shipped by both {owners[role]} and "
                    f"feature {contribution.feature!r}. Ansible resolves a role name "
                    "against the first directory that has it, so one of them "
                    "would silently replace the other; rename the role in the "
                    "package that added it."
                )
            owners[role] = f"feature {contribution.feature!r}"
        search.append(path)
    return tuple(search)


def resolve_edge_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The contributed roles core's edge play runs, before rendering."""
    return _resolve_slot(contributions, "edge_roles")


def resolve_host_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The contributed roles the edge play runs in its host slot, at the end."""
    return _resolve_slot(contributions, "host_roles")


def resolve_teardown_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The contributed roles the decommission play runs.

    Not the reverse of convergence order, and need not be: each role removes only
    what its own feature wrote.
    """
    return _resolve_slot(contributions, "teardown_roles")


def _resolve_slot(
    contributions: Iterable[AnsibleContribution], slot: str
) -> tuple[str, ...]:
    """Compose one slot: by feature name, then each feature's declared order.

    A name the feature does not ship is refused before the play starts — in the
    decommission slot, the alternative is failing half-way through taking a host
    apart.
    """
    roles: list[str] = []
    for contribution in sorted(contributions, key=lambda item: item.feature):
        requested: tuple[str, ...] = getattr(contribution, slot)
        if not requested:
            continue
        available = set(_role_names(contribution.roles_path))
        for role in requested:
            if role not in available:
                raise FeatureError(
                    f"feature {contribution.feature!r} asks for the role {role!r} "
                    f"in the {slot!r} slot, which its own roles directory "
                    f"{contribution.roles_path} does not contain."
                )
            roles.append(role)
    return tuple(roles)


def _role_names(path: Path) -> Sequence[str]:
    """Every role directory directly under ``path``, sorted; none if it is missing."""
    if not path.is_dir():
        return ()
    return sorted(entry.name for entry in path.iterdir() if entry.is_dir())
