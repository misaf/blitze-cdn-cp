"""DNS records and the site derivation that turns proxied ones into vhosts."""

from __future__ import annotations

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
    domains, records = repository.decode_snapshot_zones(snapshot)
    assert [domain.name for domain in domains] == ["example.com"]
    assert [record.fqdn for record in records] == ["cdn.example.com"]
    assert [site.name for site in repository.decode_snapshot(snapshot)] == [
        "cdn-example-com"
    ]
