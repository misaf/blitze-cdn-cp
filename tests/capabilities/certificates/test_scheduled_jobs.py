"""The intervals this capability's scheduled jobs actually run on.

An API process given this capability's settings registers its jobs on the
schedule those settings ask for. A schedule is a row, so this reads what is
registered, how often, and with what lease.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from support.control_plane import with_capability_settings

from blitzecdn.composition import Repository
from blitzecdn.entrypoints.api import create_app


def schedules(settings) -> dict[str, tuple[int, int]]:
    """What the API registered, by name, as ``(interval, lease)``.

    Read from the database, since a schedule must outlive the process.
    """
    with TestClient(create_app(settings)):
        pass
    return {
        item.name: (item.interval_seconds, item.lease_seconds)
        for item in Repository(settings.database_path).schedules.list_schedules()
    }


def test_api_service_registers_certificate_reconciliation_on_its_interval(settings):
    registered = schedules(
        with_capability_settings(settings, certificate_reconcile_interval_seconds=60)
    )

    assert registered["certificate-reconciliation"][0] == 60
    # A job with no opinion of its own leases for twice its interval: a run
    # that has not finished by then is stuck rather than slow.
    assert registered["certificate-reconciliation"][1] >= 120


def test_api_service_registers_automatic_ssl_scans_on_their_interval(settings):
    registered = schedules(
        with_capability_settings(
            settings,
            certificate_reconcile_interval_seconds=0,
            certificate_renewal_interval_seconds=0,
            ssl_automatic_scan_interval_seconds=90,
        )
    )

    assert registered["automatic-ssl-scan"][0] == 90
    # An interval of zero is how a job is turned off, and a disabled job
    # contributes no row at all rather than one the tick keeps skipping.
    assert "certificate-reconciliation" not in registered
    assert "certificate-renewal" not in registered
