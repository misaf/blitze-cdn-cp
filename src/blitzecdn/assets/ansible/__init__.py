"""The platform's Ansible tree — roles, plays, inventory plugin and defaults.

Shipped inside the package and located through
:func:`blitzecdn.core.runtime.resources.package_directory`, so an installed
controller with no checkout can converge.
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


#: The platform roles: first on the role search path, never shadowed.
ROLES_PATH = _DIRECTORY / "roles"

#: Passed straight to ``PlaybookRunner.run_playbook``.
EDGE_PLAYBOOK = _DIRECTORY / "playbooks" / "edge.yml"
DECOMMISSION_PLAYBOOK = _DIRECTORY / "playbooks" / "decommission.yml"

#: Not run by the control plane: ``install.sh`` converges the controller's own
#: host with these.
CONTROL_PLANE_PLAYBOOK = _DIRECTORY / "playbooks" / "control-plane.yml"
UNINSTALL_PLAYBOOK = _DIRECTORY / "playbooks" / "uninstall.yml"

#: The inventory plugin directory and the source file selecting it; Ansible
#: loads inventory plugins by directory.
INVENTORY_PLUGINS_PATH = _DIRECTORY / "plugins" / "inventory"
INVENTORY_PATH = _DIRECTORY / "inventory" / "blitzecdn.yml"

#: The third-party collections the platform roles depend on. Fetched at install
#: time, never vendored, and pinned in the file itself.
REQUIREMENTS_PATH = _DIRECTORY / "requirements.yml"
