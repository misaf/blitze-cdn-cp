"""Where the application is put together, and the only place that decides.

``roster`` lists the features and what each binds; ``infrastructure`` builds
the concrete adapters; ``services`` the service graph; ``processes`` one
container per process; ``installation`` database maintenance; ``repository``
which stores share the database. Import only the names re-exported here.
"""

from blitzecdn.composition.infrastructure import FleetRunner, Infrastructure
from blitzecdn.composition.installation import migrate_database
from blitzecdn.composition.processes import (
    ApiServices,
    CliServices,
    HandoffServices,
    WorkerServices,
    build_api,
    build_cli,
    build_install_handoff,
    build_worker,
)
from blitzecdn.composition.repository import Repository
from blitzecdn.composition.roster import (
    BUILTIN_FEATURES,
    ROSTER,
    RosterEntry,
    load_registry,
)
from blitzecdn.composition.services import ApplicationServices, build_services

__all__ = [
    "BUILTIN_FEATURES",
    "ROSTER",
    "ApiServices",
    "ApplicationServices",
    "CliServices",
    "FleetRunner",
    "HandoffServices",
    "Infrastructure",
    "Repository",
    "RosterEntry",
    "WorkerServices",
    "build_api",
    "build_cli",
    "build_install_handoff",
    "build_services",
    "build_worker",
    "load_registry",
    "migrate_database",
]
