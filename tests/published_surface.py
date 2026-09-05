"""What an installed capability may name inside the control plane.

Three tuples, one policy, and both suites that enforce it read them from here.

`test_packages` asks whether any wheel in the workspace imports something
outside the allowlist. `contract/test_frozen` expands the same prefixes into the
symbols behind them and pins those, because a prefix decides which *modules* a
package may import and says nothing about what is in them — a name an installed
package uses can be renamed with the allowlist untouched.

They were constants inside `test_packages`, which made the second reader import
a test module to reach them. A promise this project makes to distributions it
does not ship is not architecture-test scaffolding; it is the policy, and it
belongs somewhere both suites can name.
"""

from __future__ import annotations

#: What an optional package may reach for inside the control plane, by module
#: prefix. Everything here is a *contract*: the plugin SDK, configuration, the
#: shared value types, the ports an installed capability is handed, and the
#: entry-layer toolkits a contributed router or command is built from.
#:
#: The exclusions are the point. `blitzecdn.composition` is the control plane's
#: composition root and a package composes itself; `blitzecdn.core.persistence`
#: and a capability's `*.persistence` are storage implementations reached
#: through ports; `blitzecdn.api.app` and `blitzecdn.cli.main` are the two
#: application compositions, and a plugin that imported either would be
#: assembling the thing that is assembling it.
#:
#: Two packages are published whole and the rest module by module, which is
#: the shape of core rather than an inconsistency. `core.domain` is values and
#: `core.ports` is protocols: everything in either is publishable by
#: construction, and a module added to one is a new value or a new protocol.
#: `test_core_domain_and_ports_are_framework_and_io_independent` is that
#: construction, and it is the reason a prefix may stand here in place of a
#: list — without it, an I/O module written into `core/domain/` would join the
#: public SDK on the day it appeared and nothing would say so.
#: `core.runtime` and `core.persistence` do I/O, so each published module there
#: is a separate promise — `resources` is one, `schema` is one, and neither
#: makes the package beside it public.
_PUBLIC_SDK_PREFIXES = (
    "blitzecdn.core.plugins",
    "blitzecdn.core.config",
    "blitzecdn.core.exceptions",
    "blitzecdn.core.domain",
    "blitzecdn.core.ports",
    "blitzecdn.core.runtime.filesystem",
    "blitzecdn.core.runtime.process",
    # How a wheel finds its own roles, plays and templates on disk, and which
    # version of itself it is. Published because every capability needs both
    # and each one used to answer them itself: eight copies of one guard, two
    # of which were never written, and eleven `__version__` literals that a
    # release could leave behind.
    "blitzecdn.core.runtime.resources",
    # The one module of persistence an installed package may name: a backup
    # records the Alembic revision it was taken at, and a restore refuses one
    # this installation has never heard of. The engine, the models and the
    # stores stay private.
    "blitzecdn.core.persistence.schema",
    "blitzecdn.api.dependencies",
    "blitzecdn.api.models",
    "blitzecdn.api.requests",
    "blitzecdn.cli.common",
)

#: A capability contract another capability owns. Allowed, and named one by one
#: rather than by a wildcard over `blitzecdn.capabilities.*`: `CdnSite` and
#: `HttpScheme` are contracts every capability already consumes, while a
#: capability's `service` or `adapters` module is not something an installed
#: package may reach into. `deployments.api.models` is listed for the same
#: reason as a contract: a capability that answers with a `Deployment` — as
#: `certificates` does after a renewal converges — publishes the shape
#: `deployments` defined rather than a second one of its own.
_PUBLIC_CAPABILITY_MODULES = (
    "blitzecdn.capabilities.sites",
    "blitzecdn.capabilities.cache.policy",
    "blitzecdn.capabilities.http.policy",
    "blitzecdn.capabilities.dns.domain",
    "blitzecdn.capabilities.dns.ports",
    "blitzecdn.capabilities.deployments.api.models",
    "blitzecdn.capabilities.deployments.domain",
    "blitzecdn.capabilities.deployments.ports",
    "blitzecdn.capabilities.edges.domain.origins",
    "blitzecdn.capabilities.edges.ports",
    "blitzecdn.capabilities.tls.policy",
    # `WorkflowKind`, so an issuance can open a journal entry of the right
    # kind. It was `blitzecdn.core.domain.operations` and needed no entry here,
    # because the journal was core's; the import did not change, only which
    # half of the workspace answers for it.
    "blitzecdn.capabilities.workflows.domain",
)

_FORBIDDEN_SDK_MODULES = (
    "blitzecdn.composition",
    "blitzecdn.worker",
    "blitzecdn.api.app",
    "blitzecdn.cli.main",
    "blitzecdn.core.persistence",
    "blitzecdn.core.ansible",
    "blitzecdn.core.runtime.broker",
)
