"""Fold what the features contribute into the process-wide values core owns.

A role name resolves against one search path, a play slot runs one ordered
list, an edge loads one `load_module` list, a fragment name and a `BLITZE_*`
variable each have one owner — none of these can belong to a single
capability, so core composes them, one module per kind.

The rules are shared. Contributions are ordered by feature name, never by
registration order, so the same features always resolve the same value. A
conflict is refused with both owners named rather than resolved first-wins. A
contribution naming something its feature does not carry is refused now, not
later by Ansible with only the role named. Nothing is copied: the package's
directory is the role.
"""

from blitzecdn.core.features.resolution.ansible import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.features.resolution.configuration import (
    CapabilityConfig,
    ResolvedCapabilityEnvironment,
    resolve_capability_environment,
    resolve_feature_configuration,
)
from blitzecdn.core.features.resolution.modules import (
    ResolvedEdgeModule,
    resolve_edge_modules,
)
from blitzecdn.core.features.resolution.nginx import (
    ResolvedNginxResource,
    resolve_nginx_resources,
)

__all__ = [
    "CapabilityConfig",
    "ResolvedCapabilityEnvironment",
    "ResolvedEdgeModule",
    "ResolvedNginxResource",
    "resolve_capability_environment",
    "resolve_edge_capability_roles",
    "resolve_edge_modules",
    "resolve_feature_configuration",
    "resolve_host_capability_roles",
    "resolve_nginx_resources",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]
