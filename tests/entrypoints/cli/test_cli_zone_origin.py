"""How a zone reaches its origin, and what repointing a record moves."""

from support.cli import (
    cli_control,
    runner,
)
from support.control_plane import (
    seed_site,
)

from blitzecdn.capabilities.serving.contract import OriginPolicy
from blitzecdn.entrypoints.cli import main as cli


def test_site_origin_sets_the_request_identity_without_moving_the_address(
    settings, monkeypatch
):
    """`domain origin` is about *as whom* the edge reaches the origin, so the
    origin itself — the record's value — is left untouched."""
    control = cli_control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    before = control.sites.get_site("example-com").origin_host

    result = runner.invoke(
        cli.app,
        [
            "domain",
            "origin",
            "example.com",
            "--request-host",
            "shared.example.net",
            "--sni",
            "tls.example.net",
        ],
    )

    assert result.exit_code == 0
    site = control.sites.get_site("example-com")
    assert site.policy[OriginPolicy].origin_request_host == "shared.example.net"
    assert site.policy[OriginPolicy].origin_sni == "tls.example.net"
    assert site.origin_host == before


def test_repointing_a_record_moves_the_sites_origin(settings, monkeypatch):
    """The site follows its record: repointing is unproxying to the new address and
    proxying again.

    The origin travels with the hostname, so nothing else in the zone moves.
    """
    control = cli_control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    assert control.sites.get_site("example-com").origin_host == "198.51.100.10"

    runner.invoke(
        cli.app, ["record", "unproxy", "example.com", "cdn", "--value", "203.0.113.40"]
    )
    result = runner.invoke(cli.app, ["record", "proxy", "example.com", "cdn", "--json"])

    assert result.exit_code == 0
    site = control.sites.get_site("example-com")
    assert site.origin_host == "203.0.113.40"
    assert site.policy[OriginPolicy].origin_request_host is None


def test_site_origin_with_no_option_says_what_to_name(settings, monkeypatch):
    control = cli_control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["domain", "origin", "example.com"])

    assert result.exit_code != 0
    assert "--request-host" in result.output


def test_site_origin_clears_an_override_it_can_set(settings, monkeypatch):
    """A setting an operator can turn on and never off is one they cannot undo."""
    control = cli_control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    runner.invoke(
        cli.app,
        ["domain", "origin", "example.com", "--request-host", "shared.example.net"],
    )
    assert (
        control.sites.get_site("example-com").policy[OriginPolicy].origin_request_host
        is not None
    )

    result = runner.invoke(
        cli.app, ["domain", "origin", "example.com", "--no-request-host"]
    )

    assert result.exit_code == 0
    assert (
        control.sites.get_site("example-com").policy[OriginPolicy].origin_request_host
        is None
    )


def test_site_origin_clearing_one_override_leaves_the_other(settings, monkeypatch):
    """The distinction the patch is built through a dict to preserve."""
    control = cli_control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    runner.invoke(
        cli.app,
        [
            "domain",
            "origin",
            "example.com",
            "--request-host",
            "shared.example.net",
            "--sni",
            "tls.example.net",
        ],
    )

    result = runner.invoke(cli.app, ["domain", "origin", "example.com", "--no-sni"])

    assert result.exit_code == 0
    site = control.sites.get_site("example-com")
    assert site.policy[OriginPolicy].origin_sni is None
    assert site.policy[OriginPolicy].origin_request_host == "shared.example.net"


def test_site_origin_refuses_a_flag_that_contradicts_its_value(settings, monkeypatch):
    control = cli_control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app,
        [
            "domain",
            "origin",
            "example.com",
            "--sni",
            "tls.example.net",
            "--no-sni",
        ],
    )

    assert result.exit_code != 0
    assert control.sites.get_site("example-com").policy[OriginPolicy].origin_sni is None
