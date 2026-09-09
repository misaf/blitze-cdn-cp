"""Builders shared by the application-service tests.

Only helpers, deliberately. This module used to re-export the domain types,
the fakes and the standard library alongside them, and every consumer said
``from application_support import *`` to get the lot — which meant a reader
could not tell where ``ControlPlane`` came from, ruff could not check an
undefined name in any of those files, and five helpers had quietly become
duplicates: three that a consumer redefined identically and so never used from
here at all, and two more copied into a module that then used neither. The
consumers import what they use from where it is defined now, and what is left
here is what nowhere else owns.
"""

import time

from control_plane_fixtures import seed_site

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.composition import ControlPlane, Repository


def _seed_proxied_record(control: ControlPlane) -> CdnSite:
    """The site and routed record most tests need: `example-com`."""
    return seed_site(control)


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
    """Wait for the workflow covering a queued run to close.

    A separate wait from `_await_terminal`: the deployment reaches a terminal
    status inside the convergence, and the workflow closes around it, so the
    two finish in that order and asserting on the second right after the first
    is a race.
    """
    deadline = time.monotonic() + timeout
    pending = {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    while time.monotonic() < deadline:
        for workflow in repository.workflows.list_workflows(10):
            if workflow.resource_id == resource_id and workflow.status not in pending:
                return workflow.status
        time.sleep(0.01)
    raise AssertionError(f"no workflow for {resource_id} finished")


__all__ = ["_await_terminal", "_await_workflow", "_seed_proxied_record"]
