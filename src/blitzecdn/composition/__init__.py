"""Where the application is put together, and the only place that decides.

* :mod:`~blitzecdn.composition.roster` — the features this application ships,
  and the rendering and operations each one binds.
* :mod:`~blitzecdn.composition.infrastructure` — the concrete adapters: the
  database, the fleet runner, the probes, the job queue.
* :mod:`~blitzecdn.composition.services` — the service graph, built once from
  the infrastructure.
* :mod:`~blitzecdn.composition.processes` — one container per process, holding
  only what that process uses, and its start and stop.
* :mod:`~blitzecdn.composition.installation` — explicit database maintenance
  without constructing application services.
* :mod:`~blitzecdn.composition.repository` — which capability stores sit on one
  SQLite database.

The processes themselves — :mod:`blitzecdn.entrypoints` — import from here. The names
re-exported below are the whole public face; nothing outside this package imports one of
its modules by name.
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
