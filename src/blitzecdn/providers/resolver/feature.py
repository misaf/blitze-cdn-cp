"""Declare the capability that decides what an edge host resolves names with.

A focused runtime provider: two Ansible roles, no desired-state contribution,
service, route or command. It always ships with the application. Its configured
enabled flag controls host DNS management; site policy never requests its token.

It is the first capability to declare all of `edge_roles` and `teardown_roles`,
and the pairing is the point rather than an accident of this package:

* Converging goes in the *edge* slot, before ``blitzecdn_firewall`` and
  ``blitzecdn_nginx``. Everything that resolves a name later depends on the
  answer — most immediately the origin hostnames the renderer writes into a
  configuration the runtime will look up.
* Withdrawing goes in the decommission slot, before ``blitzecdn_edge_teardown``.
  A drop-in under ``/etc/systemd/resolved.conf.d`` is not in any tree core
  removes, and it is not a systemd unit matching the managed prefix either, so
  a decommission that did not run this role would leave a host resolving
  through servers chosen by a control plane that has forgotten it exists.

Neither position is a preference this package expresses; core enforces both by
position in its own plays and never learns either role's name.
"""

from __future__ import annotations

from blitzecdn.core.features import AnsibleContribution, Feature, FeatureMetadata
from blitzecdn.providers.resolver import __version__, ansible

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="resolver",
            version=__version__,
            provides=frozenset({"resolver"}),
            summary="Host DNS resolution an edge can trust, and its removal.",
        ),
        ansible=(
            AnsibleContribution(
                feature="resolver",
                roles_path=ansible.ROLES_PATH,
                edge_roles=ansible.EDGE_ROLES,
                teardown_roles=ansible.TEARDOWN_ROLES,
            ),
        ),
    ),
)
