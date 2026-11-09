"""The intervals this capability's scheduled jobs actually run on.

An API process given this capability's settings registers its jobs on the
schedule those settings ask for. A schedule is a row, so this reads what is
registered, how often, and with what lease.
"""

from __future__ import annotations

from support.control_plane import registered_schedules, with_capability_settings


def test_api_service_registers_certificate_reconciliation_on_its_interval(settings):
    registered = registered_schedules(
        with_capability_settings(settings, certificate_reconcile_interval_seconds=60)
    )

    assert registered["certificate-reconciliation"][0] == 60
    # A job with no opinion of its own leases for twice its interval: a run
    # that has not finished by then is stuck rather than slow.
    assert registered["certificate-reconciliation"][1] >= 120
