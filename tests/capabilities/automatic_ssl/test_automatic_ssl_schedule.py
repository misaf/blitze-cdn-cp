"""The interval the Automatic SSL/TLS scan actually runs on.

An API process given this capability's settings registers the scan on the
schedule those settings ask for, read back from the schedules table.
"""

from __future__ import annotations

from support.control_plane import registered_schedules, with_capability_settings


def test_api_service_registers_automatic_ssl_scans_on_their_interval(settings):
    registered = registered_schedules(
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
