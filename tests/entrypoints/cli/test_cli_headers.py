"""The `BZ-*` visitor-header switches and what they report."""

import json

from cli_support import (
    _control,
    runner,
)
from control_plane_fixtures import (
    seed_site,
)

from blitzecdn.cli import main as cli


def test_site_visitor_headers_command(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    site = control.sites.get_site("example-com")
    assert site.visitor_headers.connecting_ip is True
    assert site.visitor_headers.ip_country is False

    result = runner.invoke(
        cli.app,
        ["domain", "visitor-headers", "example.com", "--ip-country", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["visitor_headers"] == {
        "connecting_ip": True,
        "ip_country": True,
    }
    assert control.sites.get_site("example-com").visitor_headers.ip_country is True

    # An option that is not named keeps its value rather than resetting it.
    narrowed = runner.invoke(
        cli.app,
        [
            "domain",
            "visitor-headers",
            "example.com",
            "--no-connecting-ip",
            "--json",
        ],
    )

    assert narrowed.exit_code == 0
    assert json.loads(narrowed.stdout)["visitor_headers"] == {
        "connecting_ip": False,
        "ip_country": True,
    }


def test_site_visitor_headers_requires_a_switch(settings, monkeypatch):
    """With no option the command would silently rewrite the block as-is."""
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["domain", "visitor-headers", "example.com"])

    assert result.exit_code != 0


def test_site_visitor_headers_reports_what_the_origin_will_see(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    enabled = runner.invoke(
        cli.app,
        ["domain", "visitor-headers", "example.com", "--ip-country"],
    )
    assert "BZ-Connecting-IP, BZ-IPCountry" in enabled.stdout

    off = runner.invoke(
        cli.app,
        [
            "domain",
            "visitor-headers",
            "example.com",
            "--no-connecting-ip",
            "--no-ip-country",
        ],
    )
    assert "no BZ-* visitor headers" in off.stdout
    assert control.sites.get_site("example-com").visitor_headers.connecting_ip is False
