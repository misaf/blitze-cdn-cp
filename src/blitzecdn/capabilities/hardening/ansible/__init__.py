"""The hardening capability's Ansible roles and plays, located inside the package."""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["HOST_ROLES", "ROLES_PATH", "TEARDOWN_ROLES"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

#: Host roles, run after the edge is serving: SSH policy after the firewall
#: validated, so a failed host is never key-only and unreachable, then Fail2Ban.
HOST_ROLES = ("blitzecdn_hardening_sshd", "blitzecdn_hardening_fail2ban")

#: The teardown role removes the jail before the SSH policy, so passwords are
#: never accepted unwatched. Core's teardown does not remove these drop-ins.
TEARDOWN_ROLES = ("blitzecdn_hardening_teardown",)
