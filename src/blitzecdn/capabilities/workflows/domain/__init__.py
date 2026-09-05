"""What a journal entry is.

`workflow.py` holds the entry, its status, one checkpoint on it, and the shape
of a kind. One module because they are one value and its parts — a `Workflow`
is not meaningful without its steps, and a `WorkflowStep` exists for nothing
else.
"""

from blitzecdn.capabilities.workflows.domain.workflow import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)

__all__ = ["Workflow", "WorkflowKind", "WorkflowStatus", "WorkflowStep"]
