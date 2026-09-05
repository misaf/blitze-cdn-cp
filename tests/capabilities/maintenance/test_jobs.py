"""Running one scheduled job, and paying off what it left owing.

These two were in `tests/platform/test_queue.py`, which is about Dramatiq: what
publishes to which queue, and that the actor names on the wire match the ones
the worker consumes. Neither of these touches a broker. They construct
`MaintenanceService` with three fakes and assert the rule the service owns, so
they were in that file only because the queue is what *calls* the service —
which is a fact about the caller, not about the code under test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.plugins import ScheduledJob


def test_a_maintenance_run_converges_what_it_left_owing():
    """The one rule that holds across every scheduled job, whoever wrote it.

    A job that changed something the fleet has not seen raises a deployment
    requirement, and the run that raised it is the run that should pay it off —
    otherwise the change sits in the database until an operator happens to
    deploy for an unrelated reason.
    """
    calls: list[str] = []
    service = MaintenanceService(
        jobs=lambda: {
            "renew-certificates": ScheduledJob(
                name="renew-certificates",
                interval_seconds=60,
                run=lambda operator: calls.append(f"renewed by {operator}"),
            )
        },
        deployments=SimpleNamespace(
            submit_deployment=lambda operator: calls.append(f"deployed by {operator}")
        ),
        requirements=SimpleNamespace(pending=lambda _kind: True),
    )

    service.run("renew-certificates")

    assert calls == ["renewed by scheduler", "deployed by scheduler"]


def test_a_run_that_left_nothing_owing_does_not_deploy():
    """The other half of the rule, and the reason it is a condition.

    Most ticks change nothing — a drift check that finds no drift, a renewal
    sweep with nothing near expiry. Converging after every one of them would
    make an idle controller deploy on a timer, so the requirement is what
    decides, and this is the case that says the check is real rather than
    always true.
    """
    calls: list[str] = []
    service = MaintenanceService(
        jobs=lambda: {
            "check-drift": ScheduledJob(
                name="check-drift",
                interval_seconds=900,
                run=lambda operator: calls.append(f"checked by {operator}"),
            )
        },
        deployments=SimpleNamespace(
            submit_deployment=lambda operator: calls.append(f"deployed by {operator}")
        ),
        requirements=SimpleNamespace(pending=lambda _kind: False),
    )

    service.run("check-drift", operator="alice")

    assert calls == ["checked by alice"]


def test_a_job_no_installed_plugin_contributes_is_refused_by_name():
    """A message can outlive the plugin that published it."""
    service = MaintenanceService(
        jobs=lambda: {},
        deployments=SimpleNamespace(submit_deployment=lambda _operator: None),
        requirements=SimpleNamespace(pending=lambda _kind: False),
    )

    with pytest.raises(NotFoundError, match="no scheduled job named 'check-drift'"):
        service.run("check-drift")


def test_the_refusal_names_what_is_installed():
    """So the operator who uninstalled the plugin can see what is left.

    `installed: none` is the empty case above; this is the one an operator
    actually hits, where a job was renamed and the message in flight still
    carries the old name.
    """
    service = MaintenanceService(
        jobs=lambda: {
            "check-drift": ScheduledJob(
                name="check-drift", interval_seconds=900, run=lambda _operator: None
            ),
            "renew-certificates": ScheduledJob(
                name="renew-certificates", interval_seconds=60, run=lambda _o: None
            ),
        },
        deployments=SimpleNamespace(submit_deployment=lambda _operator: None),
        requirements=SimpleNamespace(pending=lambda _kind: False),
    )

    with pytest.raises(NotFoundError, match="check-drift, renew-certificates"):
        service.run("renew-certs")


def test_the_job_table_is_resolved_per_run_not_at_construction():
    """The reason the port is a callable and not a mapping.

    The service is built by the composition root, and so are the plugins that
    contribute jobs. A table captured at construction would be whatever had
    registered by the time this object was made, and a job contributed after it
    would be unreachable — which is the ordering the composition root actually
    has, because this service is one of the things it builds.
    """
    table: dict[str, ScheduledJob] = {}
    ran: list[str] = []
    service = MaintenanceService(
        jobs=lambda: table,
        deployments=SimpleNamespace(submit_deployment=lambda _operator: None),
        requirements=SimpleNamespace(pending=lambda _kind: False),
    )

    with pytest.raises(NotFoundError):
        service.run("check-drift")

    table["check-drift"] = ScheduledJob(
        name="check-drift",
        interval_seconds=900,
        run=lambda operator: ran.append(operator),
    )
    service.run("check-drift")

    assert ran == ["scheduler"]
