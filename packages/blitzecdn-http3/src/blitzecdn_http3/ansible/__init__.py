"""This capability's Ansible implementation, and where it landed on disk.

The role that converges the edge's QUIC listener state ships *inside this
wheel*, beside the Python that derives it. That is what makes the capability a
whole vertical slice: installing the distribution brings the edge
implementation with it, and uninstalling takes it away, with no directory in
the control plane's checkout to add to or prune.

Located through :func:`blitzecdn.core.runtime.resources.package_directory` rather than
by counting ``..`` from ``__file__``. This module used to do the latter — three
lines and no check — which was correct in a checkout and correct in an ordinary
wheel, and silent in exactly the case the check exists for: a distribution that
is not unpacked, where Ansible is handed something that is not a filesystem
path at all.
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
EDGE_ROLE = "blitzecdn_http3"
