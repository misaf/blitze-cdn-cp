"""Where Ansible looks for roles, composed from core and from what is installed.

The role search path is the one Ansible input that is genuinely global: a play
names a role, and Ansible resolves that name against a single process-wide
list. Core therefore has to compose it, and this module is the whole of that
composition — core's own roles first, then one directory per installed plugin
that ships roles inside its own wheel.

What it deliberately does not do is copy anything. A staging directory would
mean the roles that actually run are a snapshot of the roles the packages
installed, which is a second source of truth and a stale one every time a
package is upgraded without a redeploy. The package's directory *is* the role.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import AnsibleContribution

__all__ = ["resolve_edge_capability_roles", "resolve_role_search_path"]

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

    Ordered by plugin name, like the search path, so a fleet converged from the
    same set of packages runs the same roles in the same order every time.
    Within one plugin the declared order is kept: a package shipping two roles
    is the only party that knows whether one has to precede the other.

    A name the contributing package does not actually ship is refused here.
    Ansible would refuse it too, but much later — after the engine is
    installed, the image pulled and the play half-way through an edge — and it
    would name only the role, not the distribution that asked for it.
    """
    roles: list[str] = []
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        available = set(_role_names(contribution.roles_path))
        for role in contribution.edge_roles:
            if role not in available:
                raise PluginError(
                    f"plugin {contribution.plugin!r} asks the edge play to run "
                    f"the role {role!r}, which its own roles directory "
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
