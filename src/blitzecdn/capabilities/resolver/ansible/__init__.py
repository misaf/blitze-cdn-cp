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

#: The role core's edge play runs for this capability, in its *edge* slot.
#:
#: The edge slot rather than the host one, because everything that resolves a
#: name afterwards depends on it: a release's configuration names origin
#: hostnames that the runtime will look up, and ``nginx -t`` runs before the host slot
#: exists. The slot's position also settles what this role may assume — the
#: container engine, the runtime image and the persistent directories are
#: already there, all of them fetched by name through whatever resolver the
#: host had before this role ran.
EDGE_ROLES = ("blitzecdn_resolver",)

#: And the role core's decommission play runs, in its teardown slot.
#:
#: The drop-in is at a path only this package knows. Core's
#: ``blitzecdn_edge_teardown`` removes the trees it wrote, the shared runtime
#: directories and every systemd unit matching the managed prefix; a file under
#: ``/etc/systemd/resolved.conf.d`` is none of those, and naming it there would
#: put this capability's path in a role that is installed whether or not this
#: capability is.
TEARDOWN_ROLES = ("blitzecdn_resolver_teardown",)
