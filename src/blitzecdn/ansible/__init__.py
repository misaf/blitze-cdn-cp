"""The platform's Ansible implementation, and where it landed on disk.

The roles that converge an edge, the plays that run them, the dynamic
inventory plugin that finds the fleet and the shipped non-secret defaults all
ship *inside this wheel*, beside the Python that asks for them — the same
contract every optional capability under ``packages/`` already keeps, and for
the same reason. Installing the distribution brings the deployment
implementation with it.

Before this module existed, core resolved all of it from ``Settings.project_dir``,
which made the repository checkout an undeclared runtime dependency of the root
distribution: ``pip install blitzecdn`` produced a control plane that could
converge nothing, and the only reason a real controller worked was that
``install.sh`` and the container image copied the tree in behind it.

Located through :func:`blitzecdn.core.runtime.resources.package_directory` rather than
by counting ``..`` from ``__file__``. The difference matters in exactly the
case that has to work: a wheel installed into a virtualenv on a controller,
where there is no repository and no working directory to be relative to.
"""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = [
    "CONTROL_PLANE_PLAYBOOK",
    "DECOMMISSION_PLAYBOOK",
    "EDGE_PLAYBOOK",
    "INVENTORY_PATH",
    "INVENTORY_PLUGINS_PATH",
    "REQUIREMENTS_PATH",
    "ROLES_PATH",
    "UNINSTALL_PLAYBOOK",
]


_DIRECTORY = package_directory(
    __name__,
    resolves=(
        "Ansible resolves its roles, plays and inventory plugins by filesystem path"
    ),
)


#: The platform roles, and the first entry of the search path that
#: :func:`blitzecdn.core.plugins.resolution.resolve_role_search_path` composes. Every
#: contributed directory is appended after it, and a capability shipping a role
#: whose name is already here is refused rather than allowed to shadow it.
ROLES_PATH = _DIRECTORY / "roles"

#: Passed straight to ``PlaybookRunner.run_playbook``.
EDGE_PLAYBOOK = _DIRECTORY / "playbooks" / "edge.yml"
DECOMMISSION_PLAYBOOK = _DIRECTORY / "playbooks" / "decommission.yml"

#: Not run by the control plane. ``install.sh`` converges the controller's own
#: host with these two, which is why they are named here rather than left for a
#: caller to spell: the installer works from an installed distribution, and
#: after the move there is no checkout for it to find them in.
CONTROL_PLANE_PLAYBOOK = _DIRECTORY / "playbooks" / "control-plane.yml"
UNINSTALL_PLAYBOOK = _DIRECTORY / "playbooks" / "uninstall.yml"

#: The dynamic inventory plugin, and the source file that selects it. Ansible
#: loads inventory plugins by *directory*, never by module path, so this is the
#: piece with no capability precedent — and the one that would leave the
#: control plane with every role resolved and no fleet to run them on.
INVENTORY_PLUGINS_PATH = _DIRECTORY / "plugins" / "inventory"
INVENTORY_PATH = _DIRECTORY / "inventory" / "blitzecdn.yml"

#: The third-party collections the platform roles depend on. Fetched at install
#: time, never vendored, and pinned in the file itself.
REQUIREMENTS_PATH = _DIRECTORY / "requirements.yml"
