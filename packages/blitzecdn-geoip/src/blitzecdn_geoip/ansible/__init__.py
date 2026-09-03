"""This capability's Ansible implementation, and where it landed on disk.

The role that provisions the GeoLite2 database, schedules its refresh and
writes the Nginx snippet defining ``$blitzecdn_country`` ships *inside this
wheel*, beside the Python that declares the capability. That is what makes the
capability a whole vertical slice: installing the distribution brings the edge
implementation with it, and uninstalling takes it away, with no directory in
the control plane's checkout to add to or prune.

Located through :func:`blitzecdn.core.resources.package_directory` rather than
by counting ``..`` from ``__file__``. The difference matters in exactly the
case that has to work: a wheel installed into a virtualenv on a controller,
where there is no repository and no working directory to be relative to.
"""

from __future__ import annotations

from blitzecdn.core.resources import package_directory

__all__ = ["EDGE_ROLE", "ROLES_PATH"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

#: The role core's edge play runs for this capability. Named once, here, so the
#: contribution and the directory cannot disagree about it.
EDGE_ROLE = "blitzecdn_geoip"
