"""DNS records: their address, and whether the edge answers instead."""

from __future__ import annotations

import pytest
from control_plane_fixtures import FakeRunner, seed_record

from blitzecdn.capabilities.deployments.domain.snapshots import (
    decode_snapshot,
    decode_snapshot_state,
)
from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    RecordPatch,
    RecordType,
    Rule,
)
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.exceptions import ConflictError, NotFoundError


def _control(settings, repository):
    return ControlPlane(settings=settings, repository=repository, runner=FakeRunner())


def _zone(control, **policy):
    control.dns.create_domain(
        Domain.model_validate({"name": "example.com", **policy}),
        "alice",
    )


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
    record = DnsRecord(domain="example.com", name=label, value="198.51.100.1")
    assert record.fqdn == expected_fqdn
    assert record.proxied


def test_a_record_always_carries_an_address():
    """``value`` is required whether the edge serves the hostname or not.

    It is the origin the edge fetches from while proxied and the DNS answer
    when not — the same field, present either way, as on Cloudflare.
    """
    with pytest.raises(ValueError, match="value"):
        DnsRecord(domain="example.com", name="x")
    with pytest.raises(ValueError, match="value"):
        DnsRecord(domain="example.com", name="x", proxied=True)


def test_a_record_is_proxied_unless_it_says_otherwise():
    """The default is the reason to put a hostname in a CDN at all."""
    assert DnsRecord(domain="example.com", name="www", value="198.51.100.1").proxied
    unproxied = DnsRecord(
        domain="example.com", name="db", proxied=False, value="198.51.100.11"
    )
    assert not unproxied.proxied


@pytest.mark.parametrize(
    ("type_", "value"),
    [
        (RecordType.A, "2001:db8::1"),
        (RecordType.AAAA, "198.51.100.5"),
        (RecordType.A, "not-an-address"),
    ],
)
def test_record_value_must_match_its_type(type_, value):
    with pytest.raises(ValueError, match="must be an IP"):
        DnsRecord(
            domain="example.com", name="x", type=type_, proxied=False, value=value
        )


def test_records_require_their_zone(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)

    with pytest.raises(NotFoundError, match="add it first"):
        control.dns.create_record(
            DnsRecord(domain="example.com", name="cdn", value="198.51.100.1"), "alice"
        )


def test_duplicate_records_conflict(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")

    with pytest.raises(ConflictError, match="already exists"):
        seed_record(control, name="cdn")

    # A different type for the same hostname is a different record.
    seed_record(control, name="cdn", record_type=RecordType.AAAA)
    assert len(repository.zones.list_records("example.com")) == 2


def test_a_proxied_hostname_cannot_point_two_families_at_two_origins(settings):
    """One virtual host has one upstream, so A and AAAA must agree where the
    edge fetches from — and an A record takes an IPv4 origin while an AAAA
    takes an IPv6 one, so only one family can be proxied.

    This is the one contradiction left, and the only one that can still be
    written. ``validation_errors`` refuses the deploy and names the hostname.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")
    seed_record(control, name="cdn", record_type=RecordType.AAAA)

    (error,) = control.dns.validation_errors()
    assert "cdn.example.com" in error
    assert "different origins" in error

    # Pointing only one family at the edge is fine, and is one host.
    control.dns.update_record(
        "example.com", "cdn", RecordType.AAAA, RecordPatch(proxied=False), "alice"
    )
    assert control.dns.validation_errors() == []
    (host,) = control.dns.list_sites()
    assert host.server_names == ("cdn.example.com",)
    assert host.origin_host == "198.51.100.10"


def test_hostnames_accumulate_and_drop_as_records_come_and_go(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")
    seed_record(control, name="www")

    (host,) = control.dns.list_sites()
    assert host.server_names == ("cdn.example.com", "www.example.com")

    control.dns.delete_record("example.com", "www", RecordType.A, "alice")
    (host,) = control.dns.list_sites()
    assert host.server_names == ("cdn.example.com",)


def test_a_zone_serving_nothing_derives_no_virtual_host(settings):
    """A server block with an empty ``server_name`` is nginx's default server."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="db", value="198.51.100.11", proxied=False)

    assert control.dns.list_sites() == []


def test_unproxying_publishes_the_address_the_record_holds(settings):
    """As on Cloudflare, grey-clouding leaves the record's own address as the
    answer. ``control.dns.unproxy`` still takes one explicitly, so the service
    does not publish the origin by accident; the record patch endpoint lets an
    operator ask for it by name.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")

    control.dns.update_record(
        "example.com", "cdn", RecordType.A, RecordPatch(proxied=False), "alice"
    )
    record = control.dns.get_record("example.com", "cdn", RecordType.A)
    assert not record.proxied
    assert record.value == "198.51.100.10"
    assert control.dns.list_sites() == []

    control.dns.unproxy("example.com", "cdn", RecordType.A, "198.51.100.20", "alice")
    record = control.dns.get_record("example.com", "cdn", RecordType.A)
    assert record.value == "198.51.100.20"


def test_proxying_keeps_the_address_as_the_origin(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn", value="198.51.100.20", proxied=False)

    record = control.dns.proxy("example.com", "cdn", RecordType.A, "alice")

    assert record.proxied
    assert record.value == "198.51.100.20"
    (site,) = control.dns.list_sites()
    assert site.server_names == ("cdn.example.com",)
    assert site.origin_host == "198.51.100.20"


def test_deleting_a_zone_takes_its_records_and_its_hosts(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")

    control.dns.delete_domain("example.com", "alice")

    assert repository.zones.list_records() == []
    assert control.dns.list_sites() == []


def test_snapshot_round_trips_zones_records_and_rules(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="api",
            match="api.example.com",
            overrides={"cache_enabled": False},
        ),
        "alice",
    )
    snapshot = repository.snapshot()

    domains, records, rules = decode_snapshot_state(snapshot)

    assert [domain.name for domain in domains] == ["example.com"]
    assert [record.fqdn for record in records] == ["cdn.example.com"]
    assert [rule.name for rule in rules] == ["api"]
    # The hosts are not in the document: they are derived from it on the way
    # out, which is why a rule matching nothing contributes none.
    assert [host.name for host in decode_snapshot(snapshot)] == ["example-com"]
