"""DNS records and the site derivation that turns proxied ones into vhosts."""

from __future__ import annotations

import json

import pytest
from conftest import FakeRunner

from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import (
    CdnSite,
    DnsRecord,
    Domain,
    RecordType,
    derive_site_name,
)
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.infrastructure.database import Repository


@pytest.mark.parametrize(
    ("label", "expected_fqdn", "expected_site"),
    [
        ("@", "example.com", "example-com"),
        ("api", "api.example.com", "api-example-com"),
        ("*", "*.example.com", "wildcard-example-com"),
        ("a.b", "a.b.example.com", "a-b-example-com"),
    ],
)
def test_derivation_covers_apex_subdomain_and_wildcard(
    label, expected_fqdn, expected_site
):
    record = DnsRecord(
        domain="example.com", name=label, value="198.51.100.10", proxied=True
    )
    assert record.fqdn == expected_fqdn
    assert record.site_name == expected_site
    site = record.to_site()
    assert site is not None
    assert site.server_names == (expected_fqdn,)
    assert site.origin_host == "198.51.100.10"


def test_unproxied_record_derives_no_site():
    """Bypassing the CDN must leave nothing behind, not a disabled site.

    A disabled site would still occupy its name and still be removed from the
    edge by name on the next run; the record should simply not be there.
    """
    record = DnsRecord(domain="example.com", name="db", value="198.51.100.11")
    assert record.to_site() is None


def test_derived_name_stays_within_the_site_name_limit():
    """Certificates are keyed by site name, so it must fit and stay stable."""
    long_fqdn = f"{'a' * 60}.{'b' * 60}.example.com"
    slug = derive_site_name(long_fqdn)
    assert len(slug) <= 63
    assert derive_site_name(long_fqdn) == slug  # deterministic
    assert slug != derive_site_name(f"{'a' * 60}.{'c' * 60}.example.com")
    CdnSite(name=slug, server_names=("x.example.com",), origin_host="198.51.100.1")


def test_label_starting_with_a_digit_still_yields_a_valid_site_name():
    slug = derive_site_name("1api.example.com")
    CdnSite(name=slug, server_names=("1api.example.com",), origin_host="198.51.100.1")


@pytest.mark.parametrize(
    ("type_", "value"),
    [
        (RecordType.A, "2001:db8::1"),
        (RecordType.AAAA, "198.51.100.10"),
        (RecordType.A, "origin.example.com"),
    ],
)
def test_record_value_must_match_its_type(type_, value):
    with pytest.raises(ValueError):
        DnsRecord(domain="example.com", name="x", type=type_, value=value)


def test_records_require_their_zone(settings):
    repository = Repository(settings.database_path)
    with pytest.raises(NotFoundError, match="does not exist"):
        repository.create_record(
            DnsRecord(domain="absent.example", name="x", value="198.51.100.1")
        )


def test_duplicate_records_conflict(settings):
    repository = Repository(settings.database_path)
    repository.create_domain(Domain(name="example.com"))
    record = DnsRecord(domain="example.com", name="api", value="198.51.100.1")
    repository.create_record(record)
    with pytest.raises(ConflictError, match="already exists"):
        repository.create_record(record)
    # A different type at the same name is a different record.
    repository.create_record(
        DnsRecord(
            domain="example.com", name="api", type=RecordType.AAAA, value="2001:db8::1"
        )
    )
    assert len(repository.list_records("example.com")) == 2


def test_snapshot_round_trips_zones(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    control.create_domain(Domain(name="example.com"), "alice")
    control.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    snapshot = repository.snapshot()
    zones = repository.decode_snapshot_zones(snapshot)
    assert zones is not None
    domains, records = zones
    assert [domain.name for domain in domains] == ["example.com"]
    assert [record.fqdn for record in records] == ["cdn.example.com"]
    assert [site.name for site in repository.decode_snapshot(snapshot)] == [
        "cdn-example-com"
    ]


def test_import_converts_pre_1_1_0_sites_into_proxied_records(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    # A database as an older release left it: sites, no domains or records.
    repository.create_site(
        CdnSite(
            name="example-com",
            server_names=("example.com",),
            origin_host="198.51.100.5",
        )
    )
    repository.create_site(
        CdnSite(
            name="cdn-example-com",
            server_names=("cdn.example.com",),
            origin_host="198.51.100.10",
            cache_valid_success="30m",
        )
    )

    result = control.import_sites(["example.com"], "alice")

    assert sorted(result["imported"]) == ["cdn.example.com", "example.com"]
    assert result["skipped"] == []
    assert result["dropped"] == []
    records = {record.name: record for record in repository.list_records()}
    assert records["@"].value == "198.51.100.5"
    assert records["cdn"].proxied is True
    assert records["cdn"].cache_valid_success == "30m"


def test_import_reports_hostname_origins_it_cannot_represent(settings):
    """An A record needs an address, so a hostname origin needs a human."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    repository.create_site(
        CdnSite(
            name="www-example-com",
            server_names=("www.example.com",),
            origin_host="origin.example.com",
        )
    )
    result = control.import_sites(["example.com"], "alice")
    assert result["imported"] == []
    assert "is a hostname" in result["skipped"][0]
    # It stops being served, and that must be said out loud rather than implied.
    assert result["dropped"] == ["www.example.com"]


def test_import_reports_sites_in_zones_not_yet_imported(settings):
    """Importing rewrites the derived table, so other zones drop out until done.

    Without this warning an operator imports one zone, deploys, and takes every
    other customer offline.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    repository.create_site(
        CdnSite(
            name="example-com",
            server_names=("example.com",),
            origin_host="198.51.100.5",
        )
    )
    repository.create_site(
        CdnSite(
            name="other", server_names=("cdn.other.net",), origin_host="198.51.100.9"
        )
    )

    # Importing one zone would destroy the other's legacy site, so it refuses.
    with pytest.raises(ConflictError, match=r"cdn\.other\.net"):
        control.import_sites(["example.com"], "alice")
    assert repository.list_records() == []  # nothing was written

    result = control.import_sites(["example.com", "other.net"], "alice")
    assert sorted(result["imported"]) == ["cdn.other.net", "example.com"]
    assert result["dropped"] == []
    assert sorted(
        name for site in repository.list_sites() for name in site.server_names
    ) == ["cdn.other.net", "example.com"]


def test_import_can_be_forced_to_drop_zones_you_leave_out(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    repository.create_site(
        CdnSite(
            name="example-com",
            server_names=("example.com",),
            origin_host="198.51.100.5",
        )
    )
    repository.create_site(
        CdnSite(
            name="other", server_names=("cdn.other.net",), origin_host="198.51.100.9"
        )
    )
    result = control.import_sites(["example.com"], "alice", force=True)
    assert result["imported"] == ["example.com"]
    assert result["dropped"] == ["cdn.other.net"]


def test_pre_records_snapshots_remain_readable(settings):
    """Deployment history written before zones existed must stay deployable.

    Upgrading must not strand an operator who needs to roll back to a release
    made before records were introduced.
    """
    repository = Repository(settings.database_path)
    legacy = json.dumps(
        [
            {
                "name": "cdn-example-com",
                "server_names": ["cdn.example.com"],
                "origin_host": "198.51.100.10",
            }
        ]
    )
    assert repository.decode_snapshot_zones(legacy) is None
    sites = repository.decode_snapshot(legacy)
    assert [site.server_names[0] for site in sites] == ["cdn.example.com"]
