"""The intervals this capability's scheduled jobs actually run on.

Neither test names ``blitzecdn_certificates``: what they assert is that an API
service, given this capability's settings, enqueues this capability's jobs. The
subject is still the capability, though, and that is what decides where the
test lives — the job names below exist only because this distribution's
``blitzecdn_scheduled_jobs`` hook contributed them, so with the wheel detached
there is nothing left to assert rather than an assertion that happens to fail.

They lived in ``tests/api/test_common.py`` and were held there by name, in a
``REQUIRES_CERTIFICATES`` set the fixtures read to decide whether to skip. The
move is what that set was standing in for.
"""

from __future__ import annotations

import threading

from control_plane_fixtures import with_capability_settings
from fastapi.testclient import TestClient

from blitzecdn.api import create_app


def test_api_service_runs_certificate_reconciliation_on_its_interval(
    settings, monkeypatch
):
    called = threading.Event()
    configured = with_capability_settings(
        settings, certificate_reconcile_interval_seconds=1
    )

    def enqueue(_url, job, *, ttl_seconds):
        assert job == "certificate-reconciliation"
        assert ttl_seconds >= 2
        called.set()
        return True

    monkeypatch.setattr("blitzecdn.scheduler.enqueue_scheduled_once", enqueue)

    with TestClient(create_app(configured)):
        assert called.wait(2)


def test_api_service_runs_automatic_ssl_scans_on_their_interval(settings, monkeypatch):
    called = threading.Event()
    configured = with_capability_settings(
        settings,
        certificate_reconcile_interval_seconds=0,
        certificate_renewal_interval_seconds=0,
        ssl_automatic_scan_interval_seconds=1,
    )

    def enqueue(_url, job, *, ttl_seconds):
        assert job == "automatic-ssl-scan"
        assert ttl_seconds >= 2
        called.set()
        return True

    monkeypatch.setattr("blitzecdn.scheduler.enqueue_scheduled_once", enqueue)

    with TestClient(create_app(configured)):
        assert called.wait(2)
