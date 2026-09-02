"""Register the capability that decides what an edge host resolves names with.

A narrow capability: two Ansible roles, no desired-state contribution, no
service, no route and no command. Nothing on ``CdnSite`` asks for it, so no
site is ever refused for its absence and ``capability_requirements`` never
derives its token — an installation converges byte-identical desired-state
documents whether or not it is attached. What attaching changes is only which
roles core's plays run on the host.

It is the first capability to declare all of `edge_roles` and `teardown_roles`,
and the pairing is the point rather than an accident of this package:

* Converging goes in the *edge* slot, before ``blitzecdn_firewall`` and
  ``blitzecdn_nginx``. Everything that resolves a name later depends on the
  answer — most immediately the origin hostnames the renderer writes into a
  configuration the runtime will look up.
* Withdrawing goes in the decommission slot, before ``blitzecdn_teardown``.
  A drop-in under ``/etc/systemd/resolved.conf.d`` is not in any tree core
  removes, and it is not a systemd unit matching the managed prefix either, so
  a decommission that did not run this role would leave a host resolving
  through servers chosen by a control plane that has forgotten it exists.

Neither position is a preference this package expresses; core enforces both by
position in its own plays and never learns either role's name.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.plugins import (
    AnsibleContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn_resolver import __version__, ansible


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="resolver",
        version=__version__,
        required=False,
        provides=frozenset({"resolver"}),
        summary="Host DNS resolution an edge can trust, and its removal.",
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The roles, and the two slots that run them. Core adds the directory to
    # Ansible's search path, adds each name to the slot that asked for it, and
    # never learns what either contains.
    return (
        AnsibleContribution(
            plugin="resolver",
            roles_path=ansible.ROLES_PATH,
            edge_roles=ansible.EDGE_ROLES,
            teardown_roles=ansible.TEARDOWN_ROLES,
        ),
    )
