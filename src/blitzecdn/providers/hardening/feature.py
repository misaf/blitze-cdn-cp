"""Declare the application's edge-host hardening provider."""

from __future__ import annotations

from blitzecdn.core.features import AnsibleContribution, Feature, FeatureMetadata
from blitzecdn.providers.hardening import __version__, ansible

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="hardening",
            version=__version__,
            provides=frozenset({"hardening"}),
            summary="Public-key-only SSH and a Fail2Ban jail on every edge host.",
        ),
        # The host slot in the edge play converges; the teardown slot in the
        # decommission play withdraws. Core never learns what either contains.
        ansible=(
            AnsibleContribution(
                feature="hardening",
                roles_path=ansible.ROLES_PATH,
                host_roles=ansible.HOST_ROLES,
                teardown_roles=ansible.TEARDOWN_ROLES,
            ),
        ),
    ),
)
