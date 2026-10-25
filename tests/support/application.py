"""Builders shared by the application-service tests.

Only helpers: consumers import domain types and fakes from where they are
defined.
"""

import time

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.composition import ApplicationServices, Repository
from support.control_plane import seed_site


def _seed_proxied_record(control: ApplicationServices) -> CdnSite:
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

    The deployment turns terminal inside the convergence and the workflow closes
    around it, so asserting on the workflow right after `_await_terminal` races.
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
