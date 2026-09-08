"""GeoIP: one lookup capability behind two unrelated settings."""

from __future__ import annotations

from pathlib import Path

import pytest

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
from lifecycle_support import (
    Environment,
    _environment,
    _uv,
)


#: A site that asks the edge which country a visitor is in, one setting at a
#: time. The header case needs `geoip` alone; either firewall list needs
#: `security` for the rule and `geoip` for the lookup, which is the layering
#: the acceptance criteria call "a dependency on a capability token".
def _country_site(**policy: object) -> dict[str, object]:
    return {
        "name": "example-com",
        "server_names": ["cdn.example.com"],
        "origin_host": "198.51.100.10",
        "compression": "off",
        "cache_enabled": False,
        **policy,
    }


_COUNTRY_HEADER = _country_site(visitor_headers={"ip_country": True})


_ALLOWED_COUNTRIES = _country_site(firewall={"allowed_countries": ["DE", "FR"]})


_DENIED_COUNTRIES = _country_site(firewall={"denied_countries": ["RU"]})


def test_a_site_that_asks_for_no_country_needs_no_geoip(core_only: Environment):
    """The ordinary case: a CDN site works with this package absent.

    Not merely "it loads" — it requires no token at all, so nothing about it is
    conditional on a wheel. Source, method and path firewall rules are in here
    deliberately: they are the non-geographical half of the same policy block,
    and they must not drag the lookup in behind them.
    """
    plain = core_only.site_capabilities({"compression": "off", "cache_enabled": False})
    filtered = core_only.site_capabilities(
        {
            "compression": "off",
            "cache_enabled": False,
            "visitor_headers": {"connecting_ip": True, "ip_country": False},
            "firewall": {
                "deny_sources": ["203.0.113.0/24"],
                "denied_methods": ["TRACE"],
            },
        }
    )

    assert plain["required"] == plain["missing"] == []
    assert filtered["required"] == ["security"]
    assert "geoip" not in filtered["missing"]


def test_core_alone_refuses_a_site_that_asks_for_the_country_header(
    core_only: Environment,
):
    """Detached, `BZ-IPCountry` is refused rather than quietly not written.

    An origin reading an absent header as "country unknown" is the failure
    this exists to prevent, and it is invisible: the site converges, the
    header never appears, and nothing reports it.
    """
    result = core_only.site_capabilities(_COUNTRY_HEADER)

    assert result["required"] == ["geoip"]
    assert result["missing"] == ["geoip"]


@pytest.mark.parametrize(
    "overrides", [_ALLOWED_COUNTRIES, _DENIED_COUNTRIES], ids=["allowed", "denied"]
)
def test_core_alone_refuses_a_site_that_asks_for_country_rules(
    core_only: Environment, overrides: dict[str, object]
):
    """Two tokens, because a country rule is a rule *and* a lookup.

    `security` owns the firewall and `geoip` owns resolving the address, and
    the site names both rather than one package answering for the other. The
    alternative — silently admitting every country a rule was meant to block —
    is a control an operator believes is in force and is not.
    """
    result = core_only.site_capabilities(overrides)

    assert result["required"] == ["geoip", "security"]
    assert result["missing"] == ["geoip", "security"]


def test_the_site_schema_is_identical_with_and_without_geoip_installed(
    core_only: Environment, attached: Environment
):
    """Country settings are core's, so the shape does not move with the wheel.

    A stored site asking for a country must load on a controller that has
    detached the capability — otherwise detaching would make a database row
    unreadable rather than a deployment refused.
    """
    detached_shape = core_only.site_capabilities(_COUNTRY_HEADER)
    attached_shape = attached.site_capabilities(_COUNTRY_HEADER)

    assert detached_shape["shape"] == attached_shape["shape"]
    assert {"visitor_headers", "firewall"} <= set(detached_shape["shape"])  # type: ignore[operator]


def test_attaching_geoip_makes_every_country_configuration_deployable(
    tmp_path: Path, wheels: dict[str, Path]
):
    """The full cycle for this capability, against real wheels.

    Both consumers at once, because "one capability, two consumers" is the
    packaging decision being made: one `pip install` has to satisfy the header
    and both firewall lists, and one `pip uninstall` has to refuse all three.
    """
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"], wheels["blitzecdn-security"])
    assert "geoip" not in environment.report()["capabilities"]
    for site in (_COUNTRY_HEADER, _ALLOWED_COUNTRIES, _DENIED_COUNTRIES):
        assert environment.site_capabilities(site)["missing"] == ["geoip"]

    environment.install(wheels["blitzecdn-geoip"])
    report = environment.report()
    assert "geoip" in report["plugins"]
    assert "geoip" in report["capabilities"]
    assert report["rejected"] == []
    for site in (_COUNTRY_HEADER, _ALLOWED_COUNTRIES, _DENIED_COUNTRIES):
        assert environment.site_capabilities(site)["missing"] == []
    _uv("pip", "check", "--python", str(environment.python))

    environment.uninstall("blitzecdn-geoip")
    after = environment.report()
    assert "geoip" not in after["capabilities"]
    assert after["rejected"] == []
    assert {"dns", "deployments", "http"} <= set(after["plugins"])  # type: ignore[arg-type]
    assert after["routes"]
    for site in (_COUNTRY_HEADER, _ALLOWED_COUNTRIES, _DENIED_COUNTRIES):
        assert environment.site_capabilities(site)["missing"] == ["geoip"]


def test_attaching_geoip_changes_no_desired_state(
    tmp_path: Path, wheels: dict[str, Path]
):
    """The extraction is architectural: the edge document does not move.

    Whether an edge resolves countries is fleet Ansible policy, not a variable
    the control plane derives, so this package contributes none — and the fleet
    document a country-aware installation renders is byte-identical before and
    after it is attached.
    """
    fleet = [
        _COUNTRY_HEADER,
        _country_site(name="beta", server_names=["b.example.com"]),
    ]
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.fleet_state(fleet)

    environment.install(wheels["blitzecdn-geoip"])

    assert environment.fleet_state(fleet) == before


def test_the_root_wheel_neither_contains_nor_requires_geoip(wheels: dict[str, Path]):
    """The dependency arrow, read off the built artefacts themselves."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = archive.namelist()
        metadata = next(name for name in names if name.endswith("METADATA"))
        requires = archive.read(metadata).decode()

    assert "blitzecdn_geoip" not in "".join(names)

    mentions = [
        line.replace("'", "").replace('"', "")
        for line in requires.splitlines()
        if line.startswith("Requires-Dist:") and "blitzecdn-geoip" in line
    ]
    assert mentions, "the root wheel does not offer the geoip extra at all"
    assert all(
        "extra == geoip" in line or "extra == all" in line for line in mentions
    ), mentions


def test_the_geoip_wheel_carries_its_own_edge_implementation(wheels: dict[str, Path]):
    """No vendored core — and the role that provisions the database, in full.

    Built, not assumed. A wheel that shipped only the `.py` files would leave
    the plugin contributing a directory that does not exist on an installed
    controller, and every deploy would fail with "the role was not found" — on
    the controller, never in this checkout.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn-geoip"]) as archive:
        names = set(archive.namelist())
        metadata = next(name for name in names if name.endswith("METADATA"))
        requires = archive.read(metadata).decode()

    assert sorted(name for name in names if name.endswith(".py")) == [
        "blitzecdn_geoip/__init__.py",
        "blitzecdn_geoip/ansible/__init__.py",
        "blitzecdn_geoip/plugin.py",
    ]
    root = "blitzecdn_geoip/ansible/roles/blitzecdn_geoip"
    assert f"{root}/tasks/main.yml" in names
    assert f"{root}/defaults/main.yml" in names
    assert f"{root}/meta/argument_specs.yml" in names
    # The updater's own Compose project and the systemd units that refresh the
    # database. A build that
    # filtered by extension would drop every one of them silently.
    for template in (
        "compose.yml.j2",
        "geoipupdate.env.j2",
        "geoipupdate.service.j2",
        "geoipupdate.timer.j2",
    ):
        assert f"{root}/templates/{template}" in names
    assert "blitzecdn_geoip/nginx/geoip-http.conf.j2" in names
    assert "blitzecdn_geoip/nginx/geoip-upstream.conf.j2" in names
    assert "Requires-Dist: blitzecdn" in requires


def test_the_security_wheel_carries_its_own_edge_implementation(
    wheels: dict[str, Path],
):
    """The njs challenge travels with the capability that declares it."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn-security"]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn_security/ansible/roles/blitzecdn_security"
    assert f"{root}/tasks/main.yml" in names
    assert f"{root}/templates/under-attack.js.j2" in names
    assert "blitzecdn_security/nginx/security-http.conf.j2" in names
