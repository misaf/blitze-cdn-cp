"""Register the capability that hardens an edge's host access.

A narrow capability: three Ansible roles, no desired-state contribution, no
service, no route and no command. Nothing on ``CdnSite`` asks for it, so no
site is ever refused for its absence and ``capability_requirements`` never
derives its token — an installation converges byte-identical desired-state
documents whether or not it is attached. What attaching changes is only which
roles core's plays run on the host.

Two slots, and the pairing is the point. Converging is contributed through
``host_roles`` rather than ``edge_roles`` because of where in the play it has
to run. A capability that puts something on an edge
*for the configuration to read* — a lookup database, an njs module, a snippet
in ``conf.d`` — must run before ``blitzecdn_nginx`` renders and validates the
tree. These two are the opposite: they touch nothing the configuration reads,
and they have to run *after* the runtime is up, because

* SSH policy must follow the firewall. A host that fails firewall validation
  must never be left key-only and unreachable from the management network,
  which is what an earlier slot would do to it.
* Fail2Ban must follow SSH, so its jail protects a daemon that has already
  stopped accepting passwords.

So the ordering is not a preference this package expresses; it is the reason
the second slot exists at all, and core enforces it by position in the play
rather than by knowing either role's name.

Withdrawing goes in the decommission slot, before ``blitzecdn_teardown``. A
drop-in under ``/etc/ssh/sshd_config.d`` and a jail under
``/etc/fail2ban/jail.d`` are not in any tree core removes, and neither is a
systemd unit matching the managed prefix. Core used to name both paths in
``blitzecdn_teardown``'s defaults and reload both services from its own
handlers, which meant a role installed on every controller carried the paths of
a capability that may not be installed — and a fleet that had detached this
distribution still had a decommission asserting against files nothing on that
controller could write.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import (
    AnsibleContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn_hardening import __version__, ansible


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="hardening",
        version=__version__,
        required=False,
        provides=frozenset({"hardening"}),
        summary="Public-key-only SSH and a Fail2Ban jail on every edge host.",
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The roles, and the two slots that run them: the host slot in the edge
    # play converges, the teardown slot in the decommission play withdraws.
    # Core adds the directory to Ansible's search path, adds each name to the
    # slot that asked for it, and never learns what any of them contains.
    return (
        AnsibleContribution(
            plugin="hardening",
            roles_path=ansible.ROLES_PATH,
            host_roles=ansible.HOST_ROLES,
            teardown_roles=ansible.TEARDOWN_ROLES,
        ),
    )
