"""What each kind of job carries: one frozen model per kind.

A job's payload crosses a process boundary and may wait in the table across a
release, so it is typed at both ends. The publisher builds the kind's model,
which is serialized as JSON; the worker validates the stored JSON against the
same model before its handler sees it. A payload that no longer fits fails the
job at once, naming the fields, instead of a ``KeyError`` deep in a handler.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

__all__ = ["DeploymentJob", "JobPayload", "ScheduledFiring"]


class JobPayload(BaseModel):
    """The arguments of one kind of job: ids and names, never desired state.

    Every concrete subclass names its ``kind``, the string stored on the job
    row and the key the worker's handlers are registered under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The job kind this payload belongs to, unique across the application.
    kind: ClassVar[str]

    def as_json(self) -> dict[str, Any]:
        """The payload as stored on the job row."""
        return self.model_dump(mode="json")


class DeploymentJob(JobPayload):
    """Converge one queued deployment."""

    kind: ClassVar[str] = "deployment"

    deployment_id: str


class ScheduledFiring(JobPayload):
    """One firing of a recurring job, by the schedule's name."""

    kind: ClassVar[str] = "scheduled"

    job: str
