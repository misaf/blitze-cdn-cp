"""Where the application is put together, and the only place that decides.

``roster`` lists the features, the provider each builds its services with, and
what each binds; ``infrastructure`` provides the concrete adapters;
``bindings`` which capability satisfies another's port; ``services`` binds the
features' behaviour; ``processes`` one dishka container per process;
``installation`` database maintenance; ``repository`` the database and the
stores no single capability owns. Import only the names re-exported here.
"""

from blitzecdn.composition.infrastructure import (
    Adapters,
    FleetRunner,
    InfrastructureProvider,
)
from blitzecdn.composition.installation import migrate_database
from blitzecdn.composition.processes import (
    ApiNode,
    ApiProvider,
    Worker,
    WorkerProvider,
    api_container,
    cli_container,
    handoff_container,
    worker_container,
)
from blitzecdn.composition.repository import Repository
from blitzecdn.composition.roster import (
    BUILTIN_FEATURES,
    ROSTER,
    RosterEntry,
    compose_registry,
    load_registry,
)
from blitzecdn.composition.services import ServiceProvider, service_providers

__all__ = [
    "BUILTIN_FEATURES",
    "ROSTER",
    "Adapters",
    "ApiNode",
    "ApiProvider",
    "FleetRunner",
    "InfrastructureProvider",
    "Repository",
    "RosterEntry",
    "ServiceProvider",
    "Worker",
    "WorkerProvider",
    "api_container",
    "cli_container",
    "compose_registry",
    "handoff_container",
    "load_registry",
    "migrate_database",
    "service_providers",
    "worker_container",
]
