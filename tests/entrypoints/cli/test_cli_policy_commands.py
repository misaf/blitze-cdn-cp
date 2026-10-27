"""Delivery submits intent without reading canonical policy to assemble it."""

import pytest
from support.cli import cli_control, runner

from blitzecdn.capabilities.dns.domain import (
    Domain,
    EditFirewall,
    EditVisitorHeaders,
    PatchPolicy,
)
from blitzecdn.entrypoints.cli import main as cli


@pytest.mark.parametrize(
    ("arguments", "command_type"),
    [
        (["firewall", "example.com", "--deny-path", "/admin"], EditFirewall),
        (["visitor-headers", "example.com", "--ip-country"], EditVisitorHeaders),
        (["under-attack", "example.com", "--on"], PatchPolicy),
    ],
)
def test_policy_edit_submits_intent_without_a_delivery_read(
    settings, monkeypatch, arguments, command_type
):
    control = cli_control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "setup")
    handler = control.dns.update_domain
    received = []

    def submit(name, command, operator):
        received.append((name, command, operator))
        return handler(name, command, operator)

    def premature_read(*args, **kwargs):
        pytest.fail("delivery must let the handler read inside its transaction")

    monkeypatch.setattr(control.dns, "update_domain", submit)
    monkeypatch.setattr(control.dns, "get_domain", premature_read)
    result = runner.invoke(cli.app, ["domain", *arguments])
    assert result.exit_code == 0, result.output
    assert len(received) == 1
    name, command, operator = received[0]
    assert (name, operator) == ("example.com", "cli")
    assert isinstance(command, command_type)
