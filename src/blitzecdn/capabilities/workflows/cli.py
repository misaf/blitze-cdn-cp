"""Reading the workflow journal, and acknowledging interrupted work."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.workflows.domain import Workflow
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator
from blitzecdn.delivery.cli import common

workflow_app = typer.Typer(
    help="Durable progress for work that crosses out of a transaction."
)


def _row(workflow: Workflow) -> dict[str, object]:
    return {
        "id": workflow.id,
        "kind": workflow.kind,
        "resource_id": workflow.resource_id,
        "status": workflow.status.value,
        "operator": workflow.operator,
        "updated_at": workflow.updated_at.isoformat(),
        "last_step": workflow.steps[-1].name if workflow.steps else None,
        "error": workflow.error,
    }


@workflow_app.command("list")
def workflow_list(
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 50,
    json_output: common.JsonOutput = False,
) -> None:
    """Recent workflows, newest first, including any that need review."""
    common.emit(
        [
            _row(workflow)
            for workflow in common.service(WorkflowHistory).list_workflows(limit)
        ],
        json_output=json_output,
    )


@workflow_app.command("show")
def workflow_show(
    workflow_id: Annotated[str, typer.Argument(help="The workflow's identifier.")],
    json_output: common.JsonOutput = False,
) -> None:
    """One workflow with every recorded checkpoint."""
    workflow = common.service(WorkflowHistory).get(workflow_id)
    common.emit(
        {
            **_row(workflow),
            "created_at": workflow.created_at.isoformat(),
            "steps": [step.model_dump(mode="json") for step in workflow.steps],
        },
        json_output=json_output,
    )


@workflow_app.command("acknowledge")
def workflow_acknowledge(
    workflow_id: Annotated[str, typer.Argument(help="The workflow's identifier.")],
    json_output: common.JsonOutput = False,
) -> None:
    """Mark a needs_review workflow reviewed, after checking its checkpoints.

    A restart during external work leaves the workflow in needs_review, and it
    is kept until acknowledged. Verify the external side first (for example,
    whether the certificate was issued), then acknowledge; it is then pruned
    like any finished workflow.
    """
    workflow = common.service(WorkflowCoordinator).acknowledge(workflow_id, "cli")
    common.emit(
        _row(workflow),
        json_output=json_output,
        note=f"\nAcknowledged {workflow.id}; it is now reviewed.",
    )
