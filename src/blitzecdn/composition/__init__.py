"""Where the application is put together, and the only place that decides.

``roster`` lists the features and what each binds; ``infrastructure`` provides
the concrete adapters; ``services`` the service graph; ``processes`` one
dishka container per process; ``installation`` database maintenance;
``repository`` which stores share the database. Import only the names
re-exported here.
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
from blitzecdn.composition.services import ServiceProvider

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
    "worker_container",
]
