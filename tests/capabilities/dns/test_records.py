"""DNS records: what they answer with, and the site they route a hostname to."""

from __future__ import annotations

import pytest
from control_plane_fixtures import FakeRunner, seed_site

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.capabilities.deployments.domain.snapshots import (
    decode_snapshot,
    decode_snapshot_state,
)
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.persistence import Repository


@pytest.mark.parametrize(
    ("label", "expected_fqdn"),
    [
        ("@", "example.com"),
        ("api", "api.example.com"),
        ("*", "*.example.com"),
        ("a.b", "a.b.example.com"),
    ],
)
def test_the_hostname_covers_apex_subdomain_and_wildcard(label, expected_fqdn):
    record = DnsRecord(domain="example.com", name=label, site="cdn-example-com")
    assert record.fqdn == expected_fqdn
    assert record.proxied


def test_a_record_answers_with_an_address_or_routes_to_a_site_never_both():
    """The switch is which field is set, and the two are mutually exclusive.

    Before the inversion this was a ``proxied`` boolean beside a ``value`` that
    meant the origin when it was true and the DNS answer when it was false. One
    field with two meanings is what let an unproxied record keep publishing the
    origin address; two fields, one of them null, cannot.
    """
    with pytest.raises(ValueError, match="exactly one"):
        DnsRecord(domain="example.com", name="x", value="198.51.100.1", site="a-site")
    with pytest.raises(ValueError, match="exactly one"):
        DnsRecord(domain="example.com", name="x")


def test_an_unrouted_record_is_not_proxied():
    record = DnsRecord(domain="example.com", name="db", value="198.51.100.11")
    assert not record.proxied
    assert record.site is None


def test_a_site_reference_must_look_like_a_site_name():
    with pytest.raises(ValueError, match="site must start with a letter"):
        DnsRecord(domain="example.com", name="x", site="Not A Site")


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
        repository.zones.create_record(
            DnsRecord(domain="absent.example", name="x", value="198.51.100.1")
        )


def test_duplicate_records_conflict(settings):
    repository = Repository(settings.database_path)
    repository.zones.create_domain(Domain(name="example.com"))
    record = DnsRecord(domain="example.com", name="api", value="198.51.100.1")
    repository.zones.create_record(record)
    with pytest.raises(ConflictError, match="already exists"):
        repository.zones.create_record(record)
    # A different type at the same name is a different record.
    repository.zones.create_record(
        DnsRecord(
            domain="example.com", name="api", type=RecordType.AAAA, value="2001:db8::1"
        )
    )
    assert len(repository.zones.list_records("example.com")) == 2


def _control(settings, repository):
    return ControlPlane(settings=settings, repository=repository, runner=FakeRunner())  # type: ignore[arg-type]


def test_routing_a_record_to_an_unknown_site_is_refused(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "alice")
    with pytest.raises(NotFoundError, match="does not exist; create it"):
        control.dns.create_record(
            DnsRecord(domain="example.com", name="www", site="absent-site"), "alice"
        )


def test_a_dual_stack_hostname_is_one_site_with_one_origin(settings):
    """The case the old model could not express, and got wrong in silence.

    Two records for one hostname used to be two sites' worth of policy and two
    origins, of which the derivation kept whichever it saw first. Here they are
    two records naming one site: one origin, one policy, one ``server_name``.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="www-example-com", record="www")
    control.dns.create_record(
        DnsRecord(
            domain="example.com",
            name="www",
            type=RecordType.AAAA,
            site="www-example-com",
        ),
        "alice",
    )
    site = control.sites.get_site("www-example-com")
    assert site.server_names == ("www.example.com",)
    assert site.origin_host == "198.51.100.10"
    assert control.dns.validation_errors() == []


def test_one_hostname_cannot_be_routed_to_two_sites(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="www-example-com", record="www")
    seed_site(control, name="other-site", routed=False)
    with pytest.raises(ConflictError, match="is already served by site"):
        control.dns.create_record(
            DnsRecord(
                domain="example.com",
                name="www",
                type=RecordType.AAAA,
                site="other-site",
            ),
            "alice",
        )


def test_hostnames_accumulate_and_drop_as_records_come_and_go(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="www-example-com", record="www")
    control.dns.create_record(
        DnsRecord(domain="example.com", name="@", site="www-example-com"), "alice"
    )
    assert control.sites.get_site("www-example-com").server_names == (
        "example.com",
        "www.example.com",
    )
    control.dns.delete_record("example.com", "@", RecordType.A, "alice")
    assert control.sites.get_site("www-example-com").server_names == (
        "www.example.com",
    )


def test_unrouting_requires_the_address_dns_should_answer_with(settings):
    """The site's origin is not silently promoted to the public answer."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="www-example-com", record="www")
    record = control.dns.stop_routing(
        "example.com", "www", RecordType.A, "203.0.113.7", "alice"
    )
    assert record.value == "203.0.113.7"
    assert record.site is None
    assert control.sites.get_site("www-example-com").server_names == ()

    # And clearing the route without naming a replacement is not expressible:
    # the record would then answer with nothing at all.
    control.dns.create_record(
        DnsRecord(domain="example.com", name="api", site="www-example-com"), "alice"
    )
    with pytest.raises(ValueError, match="exactly one"):
        control.dns.update_record(
            "example.com", "api", RecordType.A, RecordPatch(site=None), "alice"
        )


def test_deleting_a_zone_takes_its_hostnames_off_the_sites(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="www-example-com", record="www")
    control.dns.delete_domain("example.com", "alice")
    site = control.sites.get_site("www-example-com")
    assert site.server_names == ()
    assert not site.serves_traffic
    assert control.dns.validation_errors() == []


def test_snapshot_round_trips_zones_records_and_sites(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="cdn-example-com", record="cdn")
    # A site nothing routes to is desired state the old snapshot could not hold.
    seed_site(control, name="staged-site", routed=False)
    snapshot = repository.snapshot()
    domains, records, sites = decode_snapshot_state(snapshot)
    assert [domain.name for domain in domains] == ["example.com"]
    assert [record.fqdn for record in records] == ["cdn.example.com"]
    assert sorted(site.name for site in sites) == ["cdn-example-com", "staged-site"]
    assert sorted(site.name for site in decode_snapshot(snapshot)) == [
        "cdn-example-com",
        "staged-site",
    ]
