"""Where a role is, and which of them core's own plays run.

Four answers from one contribution, and they are four rather than one because
Ansible asks them in four different places. ``resolve_role_search_path``
answers where a role *is* — a single process-wide list, so a name resolving to
two directories is a package silently replacing ``blitzecdn_nginx``. The three
slot resolvers answer which contributed roles a *play* includes, and a role
belongs to exactly one slot: two positions in the edge play, one in the
decommission play.

A package that ships a role only its own plays reach declares the directory
and no slot at all, which is why the two questions cannot be one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import AnsibleContribution

__all__ = [
    "resolve_edge_capability_roles",
    "resolve_host_capability_roles",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]


#: How core is named in a conflict message. Not a plugin name — nothing
#: registers under it — only the label the failure needs to name both sides.
_CORE = "the control plane"


def resolve_role_search_path(
    core_roles: Path, contributions: Iterable[AnsibleContribution]
) -> tuple[Path, ...]:
    """Core's roles directory, then each contributor's, deterministically.

    Ordering is core first and then contributions by plugin name, so the path a
    deployment runs against depends on *what is installed* and never on the
    order pluggy happened to register it in. Two edges converged from the same
    set of packages resolve every role identically.

    A role name that appears in two directories is refused with both owners
    named, rather than silently shadowed. Shadowing is the failure this exists
    to prevent: Ansible takes the first match, so a package shipping
    ``blitzecdn_nginx`` would replace the edge's configuration renderer and the
    deployment would succeed while converging something nobody wrote.

    A contributed directory that is not there is refused too. It means the
    package declared a role tree its wheel did not carry, and the alternative
    to failing here is a play that fails much later with "the role was not
    found", naming nothing that would lead anyone to the package.
    """
    ordered = sorted(contributions, key=lambda item: item.plugin)
    search: list[Path] = [core_roles]
    owners: dict[str, str] = dict.fromkeys(_role_names(core_roles), _CORE)
    for contribution in ordered:
        path = contribution.roles_path
        if not path.is_dir():
            raise PluginError(
                f"plugin {contribution.plugin!r} contributes the Ansible roles "
                f"directory {path}, which does not exist. Its distribution was "
                "built without its Ansible resources, or was installed in a way "
                "that did not carry them."
            )
        for role in _role_names(path):
            if role in owners:
                raise PluginError(
                    f"role {role!r} is shipped by both {owners[role]} and "
                    f"plugin {contribution.plugin!r}. Ansible resolves a role name "
                    "against the first directory that has it, so one of them "
                    "would silently replace the other; rename the role in the "
                    "package that added it."
                )
            owners[role] = f"plugin {contribution.plugin!r}"
        search.append(path)
    return tuple(search)


def resolve_edge_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """Which contributed roles core's edge play runs, in a fixed order.

    The companion to the search path, and separate from it for the same reason
    the two questions are separate: the search path says where a role *is*, and
    this says which ones the shared edge play *includes*. A package that ships
    a role only its own plays reach — a purge, a statistics collection —
    answers this with nothing and converges no edge on a deploy.
    """
    return _resolve_slot(contributions, "edge_roles")


def resolve_host_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The same question for the play's *host* slot, at the end of the run.

    A separate list rather than a flag on the names in one, because the two
    slots are two positions in a play and a role belongs to exactly one of
    them. Composed identically, and deliberately by the same private helper:
    the ordering rule, the "your wheel does not ship that" refusal and the
    determinism are properties of *a slot*, and a second copy of them would be
    a second place for them to drift.
    """
    return _resolve_slot(contributions, "host_roles")


def resolve_teardown_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The same question again, for the decommission play's slot.

    A third list rather than a reuse of either edge slot, because a
    decommission is a different play with a different guarantee: it runs on a
    host that is about to leave inventory, and after it there is no way back.
    A capability removes what it wrote here or it stays on the host forever.

    Composed by the same helper for the same reason as the other two, and
    ordered by plugin name like them — with one consequence worth naming.
    Removal order is not the reverse of convergence order and does not need to
    be: each role withdraws only what its own package wrote, and nothing in
    this slot may depend on another capability's files still being present.
    """
    return _resolve_slot(contributions, "teardown_roles")


def _resolve_slot(
    contributions: Iterable[AnsibleContribution], slot: str
) -> tuple[str, ...]:
    """Compose one of core's capability slots.

    Ordered by plugin name, like the search path, so a fleet converged from the
    same set of packages runs the same roles in the same order every time. This
    ordering has no dependency semantics: optional roles must depend only on
    established core prerequisites. Within one plugin the declared order is
    kept because that package alone owns those roles — which is how
    ``blitzecdn-hardening`` gets Fail2Ban after SSH without core knowing that
    either exists.

    A name the contributing package does not actually ship is refused here.
    Ansible would refuse it too, but much later — after the engine is
    installed, the image pulled and the play half-way through an edge — and it
    would name only the role, not the distribution that asked for it. In the
    decommission slot that lateness is worse still: the play would have already
    started taking the host apart.
    """
    roles: list[str] = []
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        requested: tuple[str, ...] = getattr(contribution, slot)
        if not requested:
            continue
        available = set(_role_names(contribution.roles_path))
        for role in requested:
            if role not in available:
                raise PluginError(
                    f"plugin {contribution.plugin!r} asks for the role {role!r} "
                    f"in the {slot!r} slot, which its own roles directory "
                    f"{contribution.roles_path} does not contain."
                )
            roles.append(role)
    return tuple(roles)


def _role_names(path: Path) -> Sequence[str]:
    """Every role directory directly under ``path``, sorted.

    A role is a directory, so a stray file is not one. Nothing here reads what
    is inside: whether a role is *valid* is Ansible's question and
    ansible-lint's, and answering it here would be a second, weaker copy of
    both. A directory that is not there contributes no names — a contributed
    one has already been refused by then, and core's is checked by
    ``Settings.validate_runtime`` where every other missing path is.
    """
    if not path.is_dir():
        return ()
    return sorted(entry.name for entry in path.iterdir() if entry.is_dir())
