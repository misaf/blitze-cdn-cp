"""Application services.

Each service orchestrates one area of the product and depends only on the
domain and on the ports in :mod:`blitzecdn.application.ports`. Nothing here imports
FastAPI, Typer, SQLite, subprocess or Ansible — the concrete adapters are
supplied by the composition root in :mod:`blitzecdn.control_plane`.

The dependency order between them is a DAG rather than a web::

    DnsService  →  DeploymentService  →  CertificateService
                              └───────→  AutomaticSslService
    EdgeOperationsService (independent)
    CacheService (independent)

A certificate is issued against a site the zone editor derived, and installing
it needs a deployment; a rollback rewrites zones and asks the zone editor to
re-derive. Nothing points back up.

Those two arrows are declared as ports too — ``ZoneEditor`` and
``DeploymentGateway`` in :mod:`blitzecdn.application.ports` — rather than as
the concrete sibling classes. They were the last dependencies in this layer
that could only be satisfied by building the real service behind them. A test
of certificate issuance otherwise needed a deployment service, a runner, and a
zone editor and a database. Naming the four methods that actually cross each
arrow ends that: the arrows are still exactly these, and now each one says how
far it reaches.
"""

from __future__ import annotations

from blitzecdn.application.automatic_ssl import AutomaticSslService
from blitzecdn.application.cache import CacheService
from blitzecdn.application.certificates import (
    CertificateExecution,
    CertificatePersistence,
    CertificateService,
)
from blitzecdn.application.deployments import (
    DeploymentExecution,
    DeploymentPersistence,
    DeploymentService,
)
from blitzecdn.application.dns import DnsService
from blitzecdn.application.edges import EdgeOperationsService

__all__ = [
    "AutomaticSslService",
    "CacheService",
    "CertificateExecution",
    "CertificatePersistence",
    "CertificateService",
    "DeploymentExecution",
    "DeploymentPersistence",
    "DeploymentService",
    "DnsService",
    "EdgeOperationsService",
]
