"""This capability's Ansible implementation, and where it landed on disk.

The three roles that govern an edge's host access ship *inside this wheel*,
beside the Python that declares the capability, so installing the distribution
brings the implementation with it and uninstalling takes it away — with no
directory in the control plane's checkout to add to or prune.

Two of them converge the host and the third withdraws what they wrote. The
third is here for the same reason ``blitzecdn-resolver``'s teardown role is in
its wheel: both files are at paths only this package knows, and core's
``blitzecdn_teardown`` used to carry them — which put a capability's paths in a
role that is installed whether or not the capability is, and left them there
for any fleet that had detached this distribution.

Located through :func:`blitzecdn.core.runtime.resources.package_directory` rather than
by counting ``..`` from ``__file__``. The difference matters in exactly the
case that has to work: a wheel installed into a virtualenv on a controller,
where there is no repository and no working directory to be relative to.
"""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["HOST_ROLES", "ROLES_PATH", "TEARDOWN_ROLES"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

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

#: And the role core's decommission play runs, in its teardown slot.
#:
#: One role for two files, because they are withdrawn together and the order
#: between them matters: the jail goes before the policy, so the host is never
#: left accepting passwords with nothing watching the attempts. Splitting it
#: per converging role would put that ordering back in core's hands, which is
#: exactly what the slot exists to avoid.
#:
#: Core's ``blitzecdn_teardown`` removes the trees it wrote, the shared runtime
#: directories and every systemd unit matching the managed prefix. A drop-in
#: under ``/etc/ssh/sshd_config.d`` and a jail under ``/etc/fail2ban/jail.d``
#: are none of those.
TEARDOWN_ROLES = ("blitzecdn_hardening_teardown",)
