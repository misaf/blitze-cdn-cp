"""BlitzeCDN does not publish DNS, and every surface that could imply it says so.

This is a test about honesty rather than about behaviour, and it is here for a
concrete reason: the limitation was true before it was stated, the tree said so
in three docstrings, and the one place an operator actually met it —
``blitzecdn record unproxy`` — told them the opposite.
"""

from __future__ import annotations

import json

from control_plane_fixtures import FakeRunner, seed_record
from typer.testing import CliRunner

from blitzecdn.capabilities.dns.domain import PUBLICATION, PublicationMode, RecordType
from blitzecdn.cli import main as cli
from blitzecdn.composition import ControlPlane, Repository

runner = CliRunner()


def control_for(settings, monkeypatch) -> ControlPlane:
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    return control


def test_the_control_plane_says_it_publishes_nothing():
    assert PUBLICATION.publishes is False
    assert PUBLICATION.mode is PublicationMode.EXTERNAL


def test_the_export_carries_the_limitation_beside_the_records(settings, monkeypatch):
    """A caller cannot consume the instructions without meeting the caveat.

    The export is desired state for a system BlitzeCDN does not operate. As a
    bare list it read as an answer to "what does DNS say", which is a question
    nothing here can answer.
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
    """The regression this pins, in the words it used to use.

    `cdn.example.com now bypasses the CDN and answers with 203.0.113.9` is a
    statement about what DNS is doing, made by the one component in the system
    that cannot make it. The record was written, nothing answered with
    anything, and the operator had been told the switch was thrown.
    """
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
    """A fleet that is otherwise ready still serves nobody without this.

    Every other line of the readiness report says whether something this
    control plane does is working. This one says that publishing DNS is not
    among them, which is the kind of gap an operator otherwise learns about
    from a customer.
    """
    control_for(settings, monkeypatch)

    report = json.loads(
        runner.invoke(cli.app, ["doctor", "--no-resolver", "--json"]).stdout
    )

    assert report["dns_publication"]["publishes"] is False
    assert report["dns_publication"]["mode"] == "external"
