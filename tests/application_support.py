"""Shared imports and builders for focused application service tests."""

# ruff: noqa: F401 -- these names are deliberately re-exported to test modules

import re
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest
import yaml
from control_plane_fixtures import (
    FakeEdgeStore,
    FakePreflight,
    FakeRunner,
    RecordingBackgroundQueue,
    RefusingBackgroundQueue,
    ansible_run,
    host_run,
    origin_report,
)

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.core.database import Repository
from blitzecdn.core.exceptions import (
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)
from blitzecdn.core.operations import WorkflowKind, WorkflowStatus
from blitzecdn.core.runs import HostRun, RunStatus
from blitzecdn.features.deployments.domain import (
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.features.http.policy import HttpScheme
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.tls.policy import CertificateMode, SslAutomaticMode, SslMode


def _seed_proxied_record(control: ControlPlane) -> DnsRecord:
    """Create the one zone and proxied record most tests need.

    Sites can no longer be inserted directly — proxying a record is the only
    way one comes into existence — so this is the shared setup for anything
    that needs `cdn-example-com` to exist.
    """
    control.dns.create_domain(Domain(name="example.com"), "alice")
    return control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )


def _automatic_origin_report(
    mode: SslMode,
    *,
    reachable: bool = True,
    tls_verified: bool | None = None,
    status: int = 200,
) -> HostRun:
    scheme = "https" if mode in {SslMode.FULL, SslMode.FULL_STRICT} else "http"
    return host_run(
        "edge-a",
        report={
            "host": "edge-a",
            "collected_at": "2026-01-01T00:00:00Z",
            "origins": [
                {
                    "site": "cdn-example-com",
                    "origin": f"198.51.100.10:{443 if scheme == 'https' else 80}",
                    "scheme": scheme,
                    "ssl_mode": mode.value,
                    "sni": "198.51.100.10" if scheme == "https" else None,
                    "reachable": str(reachable),
                    "tls_verified": (
                        "None" if tls_verified is None else str(tls_verified)
                    ),
                    "status": str(status) if reachable else "-1",
                    "content_sha256": "a" * 64 if reachable else None,
                    "detail": "",
                }
            ],
        },
    )


def _seed_automatic_ssl_record(
    control: ControlPlane,
    *,
    mode: SslMode = SslMode.OFF,
    automatic: SslAutomaticMode = SslAutomaticMode.AUTO,
) -> None:
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord.model_validate(
            {
                "domain": "example.com",
                "name": "cdn",
                "value": "198.51.100.10",
                "proxied": True,
                "ssl_mode": mode,
                "ssl_automatic_mode": automatic,
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
            }
        ),
        "alice",
    )


class _RecordingIssuer:
    """Stands in for certbot: hands back a fresh pair and remembers the call."""

    def __init__(self, certificate_pair, *, fails: set[str] | None = None) -> None:
        self._pair = certificate_pair
        self._fails = fails or set()
        self.issued: list[tuple[str, str]] = []

    def issue(self, site, email):
        if site.name in self._fails:
            raise ExecutionError("challenge failed")
        self.issued.append((site.name, email))
        return self._pair((site.server_names[0],), days=90)


def _proxied_site_with_certificate(control, repository, certificate_pair, *, days):
    record = _seed_proxied_record(control)
    certificate, key = certificate_pair((record.fqdn,), days=days)
    return control.certificates.upload_certificate(
        record.site_name, certificate, key, "alice"
    )


def _purge_run():
    return ansible_run(host_run("edge-a", changed=1))


def _report(cache, *, reachable=True):
    return {
        "host": "ignored",
        "collected_at": "2026-08-09T01:00:00Z",
        "nginx_reachable": reachable,
        "connections": {"active": 5, "requests": 100},
        "cache": cache,
    }


def _reporting(name, cache):
    return host_run(name, ok=5, report=_report(cache))


def _await_terminal(
    repository: Repository, deployment_id: str, timeout: float = 5.0
) -> DeploymentStatus:
    deadline = time.monotonic() + timeout
    pending = {DeploymentStatus.QUEUED, DeploymentStatus.RUNNING}
    while time.monotonic() < deadline:
        status = repository.deployments.get_deployment(deployment_id).status
        if status not in pending:
            return status
        time.sleep(0.01)
    raise AssertionError(f"deployment {deployment_id} never finished")


def _await_workflow(
    repository: Repository, resource_id: str, timeout: float = 5.0
) -> WorkflowStatus:
    deadline = time.monotonic() + timeout
    pending = {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    while time.monotonic() < deadline:
        for workflow in repository.workflows.list_workflows(10):
            if workflow.resource_id == resource_id and workflow.status not in pending:
                return workflow.status
        time.sleep(0.01)
    raise AssertionError(f"no workflow for {resource_id} finished")


__all__ = [name for name in globals() if not name.startswith("__")]
