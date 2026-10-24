"""Ansible resources owned by security.

Resolved through importlib resources so the installed application works without
a repository or a particular working directory. Role and playbook names remain
stable; their files travel beside their owning implementation in the one wheel.
"""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["EDGE_ROLE", "ROLES_PATH"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

#: The role core's edge play runs for this capability. Named once, here, so the
#: contribution and the directory cannot disagree about it.
EDGE_ROLE = "blitzecdn_security"
