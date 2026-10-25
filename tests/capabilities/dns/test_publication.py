"""BlitzeCDN does not publish DNS, and every surface that could imply it says so."""

from __future__ import annotations

import json

from support.control_plane import FakeRunner, build_application, seed_record
from typer.testing import CliRunner

from blitzecdn.capabilities.dns.domain import PUBLICATION, PublicationMode, RecordType
from blitzecdn.composition import ApplicationServices, Repository
from blitzecdn.entrypoints.cli import main as cli

runner = CliRunner()


def control_for(settings, monkeypatch) -> ApplicationServices:
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "services", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    return control


def test_the_control_plane_says_it_publishes_nothing():
    assert PUBLICATION.publishes is False
    assert PUBLICATION.mode is PublicationMode.EXTERNAL


def test_the_export_carries_the_limitation_beside_the_records(settings, monkeypatch):
    """A caller cannot consume the instructions without meeting the caveat.

    The export is desired state for a system BlitzeCDN does not operate.
    """
    control = control_for(settings, monkeypatch)
    seed_record(control, name="cdn")

    exported = control.dns.dns_export()

    assert exported["publication"]["publishes"] is False
    assert [record["fqdn"] for record in exported["records"]] == ["cdn.example.com"]


def test_a_proxied_record_asks_for_an_edge_address_rather_than_naming_one(
    settings, monkeypatch
):
    """The origin never leaves this side, and the edge address is not ours."""
    control = control_for(settings, monkeypatch)
    seed_record(control, name="cdn", value="198.51.100.10")

    (record,) = control.dns.dns_export()["records"]

    assert record["answer"] == "an edge address"
    assert "value" not in record


def test_unproxying_does_not_claim_that_dns_now_answers(settings, monkeypatch):
    """Unproxying records the change without claiming DNS now answers with it."""
    control = control_for(settings, monkeypatch)
    seed_record(control, name="cdn")

    result = runner.invoke(
        cli.app,
        ["record", "unproxy", "example.com", "cdn", "--value", "203.0.113.9"],
    )

    assert result.exit_code == 0
    assert "now bypasses the CDN and answers with" not in result.output
    assert "does not publish it" in result.output
    assert "203.0.113.9" in result.output
    # And the record itself did change, so the command still did its half.
    assert control.dns.get_record("example.com", "cdn", RecordType.A).proxied is False


def test_doctor_reports_that_dns_publication_is_somebody_elses(settings, monkeypatch):
    """A fleet that is otherwise ready still serves nobody without this."""
    control_for(settings, monkeypatch)

    report = json.loads(
        runner.invoke(cli.app, ["doctor", "--no-resolver", "--json"]).stdout
    )

    assert report["dns_publication"]["publishes"] is False
    assert report["dns_publication"]["mode"] == "external"
