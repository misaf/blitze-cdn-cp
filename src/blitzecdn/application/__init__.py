"""Application services.

Each service orchestrates one area of the product and depends only on the
domain and on the ports in :mod:`blitzecdn.ports`. Nothing here imports
FastAPI, Typer, SQLite, subprocess or Ansible — the concrete adapters are
supplied by the composition root in :mod:`blitzecdn.control_plane`.

The dependency order between them is a DAG rather than a web::

    DnsService  →  DeploymentService  →  CertificateService
    EdgeOperationsService (independent)

A certificate is issued against a site the zone editor derived, and installing
it needs a deployment; a rollback rewrites zones and asks the zone editor to
re-derive. Nothing points back up.
"""

from __future__ import annotations

from blitzecdn.application.certificates import CertificateService
from blitzecdn.application.deployments import DeploymentService
from blitzecdn.application.dns import DnsService
from blitzecdn.application.edges import EdgeOperationsService

__all__ = [
    "CertificateService",
    "DeploymentService",
    "DnsService",
    "EdgeOperationsService",
]
