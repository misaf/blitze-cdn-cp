"""This capability's Ansible implementation, and where it landed on disk.

Two roles ship *inside this wheel*, beside the Python that declares the
capability: the one that points a host at resolvers it can trust, and the one
that takes that drop-in off again when the host is decommissioned. Installing
the distribution brings both with it and uninstalling takes them away, with no
directory in the control plane's checkout to add to or prune.

Located through :func:`blitzecdn.core.runtime.resources.package_directory` rather than
by counting ``..`` from ``__file__``. The difference matters in exactly the
case that has to work: a wheel installed into a virtualenv on a controller,
where there is no repository and no working directory to be relative to.
"""

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
#: name afterwards depends on it: ``blitzecdn_nginx`` renders origin hostnames
#: that the runtime will look up, and ``nginx -t`` runs before the host slot
#: exists. The slot's position also settles what this role may assume — the
#: container engine, the runtime image and the persistent directories are
#: already there, all of them fetched by name through whatever resolver the
#: host had before this role ran. Managing resolution has never been what made
#: those fetches work, and moving into this slot does not change that.
EDGE_ROLES = ("blitzecdn_resolver",)

#: And the role core's decommission play runs, in its teardown slot.
#:
#: The drop-in is at a path only this package knows. Core's
#: ``blitzecdn_teardown`` removes the trees it wrote, the shared runtime
#: directories and every systemd unit matching the managed prefix; a file under
#: ``/etc/systemd/resolved.conf.d`` is none of those, and naming it there would
#: put this capability's path in a role that is installed whether or not this
#: capability is.
TEARDOWN_ROLES = ("blitzecdn_resolver_teardown",)
