"""Declare the capability that decides what an edge host resolves names with.

Infrastructure only: an edge-slot role, ahead of the firewall and nginx since
they resolve names, and a teardown role, since core's teardown would leave the
resolved drop-in pointing at servers nobody manages any more.
"""

from __future__ import annotations

from blitzecdn.capabilities.resolver import __version__, ansible
from blitzecdn.core.features import AnsibleContribution, Feature, FeatureMetadata

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
