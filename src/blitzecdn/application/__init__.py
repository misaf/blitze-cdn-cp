"""Application services.

Each service orchestrates one area of the product and depends only on the
domain and on the ports in :mod:`blitzecdn.ports`. Nothing here imports
FastAPI, Typer, SQLite, subprocess or Ansible — the concrete adapters are
supplied by the composition root in :mod:`blitzecdn.control_plane`.

The dependency order between them is a DAG rather than a web::

    DnsService  →  DeploymentService  →  CertificateService
    EdgeOperationsService (independent)
    CacheService (independent)

A certificate is issued against a site the zone editor derived, and installing
it needs a deployment; a rollback rewrites zones and asks the zone editor to
re-derive. Nothing points back up.

Those two arrows are declared as ports too — ``ZoneEditor`` and
``DeploymentGateway`` in :mod:`blitzecdn.ports` — rather than as the concrete
sibling classes. They were the last dependencies in this layer that could only
be satisfied by building the real service behind them, which meant a test of
certificate issuance needed a deployment service, which needed a runner and a
zone editor and a database. Naming the four methods that actually cross each
arrow ends that: the arrows are still exactly these, and now each one says how
far it reaches.
"""

from __future__ import annotations

from blitzecdn.application.cache import CacheService
from blitzecdn.application.certificates import CertificateService
from blitzecdn.application.deployments import DeploymentService
from blitzecdn.application.dns import DnsService
from blitzecdn.application.edges import EdgeOperationsService

__all__ = [
    "CacheService",
    "CertificateService",
    "DeploymentService",
    "DnsService",
    "EdgeOperationsService",
]
