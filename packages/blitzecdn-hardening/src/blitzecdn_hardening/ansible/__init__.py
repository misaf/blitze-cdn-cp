"""This capability's Ansible implementation, and where it landed on disk.

The two roles that harden an edge's host access ship *inside this wheel*,
beside the Python that declares the capability, so installing the distribution
brings the implementation with it and uninstalling takes it away — with no
directory in the control plane's checkout to add to or prune.

Located through :mod:`importlib.resources` rather than by counting ``..`` from
``__file__``. The difference matters in exactly the case that has to work: a
wheel installed into a virtualenv on a controller, where there is no repository
and no working directory to be relative to.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["HOST_ROLES", "ROLES_PATH"]


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
            "blitzecdn-hardening must be installed as an unpacked distribution: "
            "Ansible resolves its roles by filesystem path, and this "
            f"installation exposes them as {type(anchor).__name__}."
        )
    return anchor


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _directory() / "roles"

#: The roles core's edge play runs for this capability, in this order.
#:
#: They are *host* roles rather than edge roles, and the distinction is the
#: whole reason the contract has two slots. Both configure the host underneath
#: the runtime rather than anything the rendered configuration reads, and both
#: have to run after the edge is already serving: SSH policy after the firewall
#: has been validated, so a host that fails firewall validation is never left
#: key-only but unreachable from the management network, and Fail2Ban after SSH
#: so its bans apply to a daemon that has already stopped accepting passwords.
HOST_ROLES = ("blitzecdn_sshd", "blitzecdn_fail2ban")
