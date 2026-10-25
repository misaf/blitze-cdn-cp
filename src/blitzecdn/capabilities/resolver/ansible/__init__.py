"""The resolver capability's Ansible roles and plays, located inside the package."""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["EDGE_ROLES", "ROLES_PATH", "TEARDOWN_ROLES"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

#: The edge slot, since everything later resolves names through it, including
#: ``nginx -t``.
EDGE_ROLES = ("blitzecdn_resolver",)

#: The teardown role removes the drop-in under ``/etc/systemd/resolved.conf.d``,
#: a path only this capability knows.
TEARDOWN_ROLES = ("blitzecdn_resolver_teardown",)
