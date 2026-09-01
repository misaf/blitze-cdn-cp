"""This capability's Ansible implementation, and where it landed on disk.

The role that provisions the GeoLite2 database, schedules its refresh and
writes the Nginx snippet defining ``$blitzecdn_country`` ships *inside this
wheel*, beside the Python that declares the capability. That is what makes the
capability a whole vertical slice: installing the distribution brings the edge
implementation with it, and uninstalling takes it away, with no directory in
the control plane's checkout to add to or prune.

Located through :mod:`importlib.resources` rather than by counting ``..`` from
``__file__``. The difference matters in exactly the case that has to work: a
wheel installed into a virtualenv on a controller, where there is no repository
and no working directory to be relative to.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["EDGE_ROLE", "ROLES_PATH"]


def _directory() -> Path:
    """This package's directory as a real filesystem path.

    Ansible opens roles by path, so a ``Traversable`` that is not one — a
    package imported from inside a zip — cannot be used at all, and saying so
    here beats a role reported missing at deploy time. Wheels are unpacked on
    install, so this is the ordinary case and not a fallback.
    """
    anchor = resources.files(__name__)
    if not isinstance(anchor, Path):
        raise RuntimeError(
            "blitzecdn-geoip must be installed as an unpacked distribution: "
            "Ansible resolves its roles by filesystem path, and this "
            f"installation exposes them as {type(anchor).__name__}."
        )
    return anchor


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _directory() / "roles"

#: The role core's edge play runs for this capability. Named once, here, so the
#: contribution and the directory cannot disagree about it.
EDGE_ROLE = "blitzecdn_geoip"
