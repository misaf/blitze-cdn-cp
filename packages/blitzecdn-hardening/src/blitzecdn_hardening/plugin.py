"""Register the capability that hardens an edge's host access.

The narrowest kind of capability there is: two Ansible roles, no desired-state
contribution, no service, no route and no command. Nothing on ``CdnSite`` asks
for it, so no site is ever refused for its absence and
``capability_requirements`` never derives its token — an installation converges
byte-identical desired-state documents whether or not it is attached. What
attaching changes is only which roles core's edge play runs on the host.

It is contributed through ``host_roles`` rather than ``edge_roles`` because of
where in the play it has to run. A capability that puts something on an edge
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
    # The roles, and the fact that the edge play should run them in its host
    # slot. Core adds the directory to Ansible's search path, adds the names to
    # that slot, and never learns what either contains.
    return (
        AnsibleContribution(
            plugin="hardening",
            roles_path=ansible.ROLES_PATH,
            host_roles=ansible.HOST_ROLES,
        ),
    )
