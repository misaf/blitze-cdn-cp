"""The Automatic SSL/TLS scan, at the service level and over HTTP.

Built through ``certificate_control_plane``, which builds the composition
root's services and substitutes only the named seam: here, the fleet runner
whose origin reports the scan reads.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from support.certificates import (
    automatic_origin_report,
    certificate_control_plane,
    seed_automatic_ssl_record,
)
from support.control_plane import (
    API_HEADERS,
    FakeRunner,
    ansible_run,
    control_plane_app,
    host_run,
    run_queued_operations,
    serve,
)
from support.stores import Stores

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.tls.contract import (
    SslAutomaticMode,
    SslMode,
    TlsPolicy,
)


@pytest.mark.parametrize(
    ("tls_verified", "expected"),
    [(True, SslMode.FULL_STRICT), (False, SslMode.FULL)],
)
def test_automatic_ssl_upgrades_to_the_strongest_fleet_verified_mode(
    settings, tls_verified, expected
):
    runner = FakeRunner(
        [
            ansible_run(automatic_origin_report(SslMode.OFF)),
            ansible_run(
                automatic_origin_report(
                    SslMode.FULL_STRICT,
                    tls_verified=tls_verified,
                )
            ),
            ansible_run(host_run("edge-a")),
        ]
    )
    repository = Stores(settings.database_path)
    control = certificate_control_plane(settings, runner=runner)
    seed_automatic_ssl_record(control)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {"example-com": expected}
    assert result.skipped == {}
    assert result.deployment is not None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    site = control.sites.get_site("example-com")
    assert site.policy[TlsPolicy].ssl_mode is expected
    assert site.policy[TlsPolicy].ssl_automatic_mode is SslAutomaticMode.AUTO
    event = next(
        event
        for event in repository.audit_log.list_audit_events(20)
        if event.action == "ssl.automatic.upgraded"
    )
    assert event.action == "ssl.automatic.upgraded"
    assert event.details["to"] == expected.value


def test_automatic_ssl_uses_flexible_when_only_http_is_healthy(settings):
    runner = FakeRunner(
        [
            ansible_run(automatic_origin_report(SslMode.OFF)),
            ansible_run(
                automatic_origin_report(
                    SslMode.FULL_STRICT,
                    reachable=False,
                )
            ),
            ansible_run(host_run("edge-a")),
        ]
    )
    control = certificate_control_plane(settings, runner=runner)
    seed_automatic_ssl_record(control)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {"example-com": SslMode.FLEXIBLE}
    assert (
        control.sites.get_site("example-com").policy[TlsPolicy].ssl_mode
        is SslMode.FLEXIBLE
    )


def test_custom_ssl_mode_is_never_scanned_or_changed(settings):
    runner = FakeRunner()
    control = certificate_control_plane(settings, runner=runner)
    seed_automatic_ssl_record(control, automatic=SslAutomaticMode.CUSTOM)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.scanned == ()
    assert result.upgraded == {}
    assert runner.playbooks == []
    assert (
        control.sites.get_site("example-com").policy[TlsPolicy].ssl_mode is SslMode.OFF
    )


def test_automatic_ssl_never_downgrades_when_strict_is_unavailable(settings):
    runner = FakeRunner(
        [
            ansible_run(
                automatic_origin_report(
                    SslMode.FULL,
                    reachable=True,
                )
            ),
            ansible_run(
                automatic_origin_report(
                    SslMode.FULL_STRICT,
                    reachable=False,
                )
            ),
        ]
    )
    control = certificate_control_plane(settings, runner=runner)
    seed_automatic_ssl_record(control, mode=SslMode.FULL)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {}
    assert "example-com" in result.skipped
    assert result.deployment is None
    assert (
        control.sites.get_site("example-com").policy[TlsPolicy].ssl_mode is SslMode.FULL
    )


def test_automatic_ssl_reconciliation_is_queued_by_the_api(
    settings, seeded, monkeypatch
):
    control, _ = seeded()
    serve(monkeypatch, control)

    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post(
            "/v3/certificates/ssl-modes/reconcile", headers=API_HEADERS
        )
        assert accepted.status_code == 202, accepted.text
        operation_id = accepted.json()["id"]
        assert accepted.headers["Location"] == f"/v3/operations/{operation_id}"

        again = client.post("/v3/certificates/ssl-modes/reconcile", headers=API_HEADERS)
        assert again.status_code == 409

        run_queued_operations(control)
        finished = client.get(f"/v3/operations/{operation_id}", headers=API_HEADERS)

    assert finished.json()["status"] == "succeeded"
    assert finished.json()["result"] == {
        "scanned": [],
        "upgraded": {},
        "skipped": {},
        "deployment": None,
    }
