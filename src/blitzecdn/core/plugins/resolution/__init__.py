"""Fold what the installed plugins contribute into the facts core owns.

Every module here answers the same question about a different subsystem:
*given the contributions of whatever happens to be installed, what is the one
process-wide value core has to hand its adapters?* Ansible resolves a role name
against a single search path. A play runs one ordered list of roles per slot.
`load_module` is a main-context directive, so an edge loads one list of dynamic
modules. An Nginx fragment name is claimed once. A `BLITZE_*` variable has one
owner. None of those is a per-capability value that a capability could own, and
that is exactly why core composes them.

The rules are the same in every one of them, and they are stated here once so
they stay the same. Contributions are ordered by plugin name, never by the
order pluggy registered them, so two controllers with the same packages
installed resolve the same value. A conflict is refused with *both* owners
named rather than resolved by first-wins, because first-wins here means a
package silently replacing `blitzecdn_nginx` or an edge loading whichever
`geoip2` was emitted first. A contribution that names something its own wheel
does not carry is refused now, with the distribution named, rather than much
later by Ansible with only the role named — and in the decommission slot, later
means after the play has begun taking a host apart.

The package is named after the job rather than after any consumer of it: a
module called `ansible/roles.py` or `nginx.py` is a name that stops being true
of the whole of what it holds the moment a resolver like the capability
*environment* one — which has nothing to do with Nginx — has to go somewhere.
One module holding every resolver loses the same thing a different way: the
shared rules above get stated once at the top, and a reader is then several
hundred lines from finding out which of them a given refusal came from.

So the split is by *what is resolved*, one module per kind, mirroring the
contribution types in :mod:`blitzecdn.core.plugins.types`:

* :mod:`~blitzecdn.core.plugins.resolution.ansible` — where a role is, and
  which roles core's own plays run in each slot;
* :mod:`~blitzecdn.core.plugins.resolution.nginx` — the configuration
  fragments the edge renderer includes;
* :mod:`~blitzecdn.core.plugins.resolution.modules` — the `load_module` set
  those fragments need, composed under the opposite conflict rule;
* :mod:`~blitzecdn.core.plugins.resolution.configuration` — the `BLITZE_*`
  namespace, scoped back to the capability that claimed each name.

The consumers are still Ansible and Nginx; the job is plugin composition, so it
stays with the plugin mechanism. Importers see no change: this package's public
surface is what the module's was, and `blitzecdn.core.plugins` re-exports it.

What none of this does is copy anything. A staging directory would mean the
roles that actually run are a snapshot of the roles the packages installed,
which is a second source of truth and a stale one every time a package is
upgraded without a redeploy. The package's directory *is* the role.
"""

from blitzecdn.core.plugins.resolution.ansible import (
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.plugins.resolution.configuration import (
    CapabilityConfig,
    ResolvedCapabilityEnvironment,
    resolve_capability_environment,
    resolve_plugin_configuration,
)
from blitzecdn.core.plugins.resolution.modules import (
    ResolvedEdgeModule,
    resolve_edge_modules,
)
from blitzecdn.core.plugins.resolution.nginx import (
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
    "resolve_host_capability_roles",
    "resolve_nginx_resources",
    "resolve_plugin_configuration",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]
