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
    FakeRunner,
    RecordingBackgroundQueue,
    RefusingBackgroundQueue,
    ansible_run,
    host_run,
    origin_report,
    seed_record,
    seed_site,
    with_capability_settings,
)

from blitzecdn.capabilities.deployments.domain import (
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn.capabilities.sites.domain import CdnSite, SitePatch
from blitzecdn.capabilities.tls.policy import CertificateMode, SslAutomaticMode, SslMode
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.domain.operations import WorkflowKind, WorkflowStatus
from blitzecdn.core.domain.runs import HostRun, RunStatus
from blitzecdn.core.exceptions import (
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)


def _seed_proxied_record(control: ControlPlane) -> CdnSite:
    """The site and routed record most tests need: `cdn-example-com`."""
    return seed_site(control)


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
