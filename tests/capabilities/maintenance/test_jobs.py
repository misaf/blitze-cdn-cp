"""Running one scheduled job, and paying off what it left owing.

These two were in `tests/platform/test_queue.py`, which is about Dramatiq: what
publishes to which queue, and that the actor names on the wire match the ones
the worker consumes. Neither of these touches a broker. They construct
`MaintenanceService` with fakes and assert the rule the service owns, so
they were in that file only because the queue is what *calls* the service —
which is a fact about the caller, not about the code under test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.features import ScheduledJob


def test_a_maintenance_run_converges_what_it_left_owing():
    """The one rule that holds across every scheduled job, whoever wrote it.

    A job that changed something the fleet has not seen raises a deployment
    requirement, and the run that raised it is the run that should pay it off —
    otherwise the change sits in the database until an operator happens to
    deploy for an unrelated reason. Whether anything is owed is the deployment
    service's answer; this asks after every run.
    """
    calls: list[str] = []
    service = MaintenanceService(
        jobs={
            "renew-certificates": ScheduledJob(
                feature="certificates",
                name="renew-certificates",
                interval_seconds=60,
                run=lambda operator: calls.append(f"renewed by {operator}"),
            )
        },
        deployments=SimpleNamespace(
            converge_requirements=lambda operator: calls.append(
                f"converged by {operator}"
            )
        ),
    )

    service.run("renew-certificates")

    assert calls == ["renewed by scheduler", "converged by scheduler"]


def test_a_job_no_installed_plugin_contributes_is_refused_by_name():
    """A message can outlive the feature that published it."""
    service = MaintenanceService(
        jobs={},
        deployments=SimpleNamespace(converge_requirements=lambda _operator: None),
    )

    with pytest.raises(NotFoundError, match="no scheduled job named 'check-drift'"):
        service.run("check-drift")


def test_the_refusal_names_what_is_installed():
    """So the operator who uninstalled the feature can see what is left.

    `installed: none` is the empty case above; this is the one an operator
    actually hits, where a job was renamed and the message in flight still
    carries the old name.
    """
    service = MaintenanceService(
        jobs={
            "check-drift": ScheduledJob(
                feature="deployments",
                name="check-drift",
                interval_seconds=900,
                run=lambda _operator: None,
            ),
            "renew-certificates": ScheduledJob(
                feature="certificates",
                name="renew-certificates",
                interval_seconds=60,
                run=lambda _o: None,
            ),
        },
        deployments=SimpleNamespace(converge_requirements=lambda _operator: None),
    )

    with pytest.raises(NotFoundError, match="check-drift, renew-certificates"):
        service.run("renew-certs")
