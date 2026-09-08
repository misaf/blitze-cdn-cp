"""DNS records: what they answer with, and whether the edge answers instead."""

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
    return ControlPlane(settings=settings, repository=repository, runner=FakeRunner())  # type: ignore[arg-type]


def _zone(control, origin: str = "198.51.100.10", **policy):
    control.dns.create_domain(
        Domain.model_validate({"name": "example.com", "origin_host": origin, **policy}),
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
    record = DnsRecord(domain="example.com", name=label)
    assert record.fqdn == expected_fqdn
    assert record.proxied


def test_the_switch_and_the_address_cannot_contradict_each_other():
    """One field says who answers, the other says with what. They must agree.

    Before the collapse this was ``value`` against a ``site`` name, and before
    that a ``proxied`` boolean beside a ``value`` that meant the origin when it
    was true and the DNS answer when it was false. One field with two meanings
    is what let an unproxied record keep publishing the origin address.
    """
    with pytest.raises(ValueError, match="cannot carry a 'value' of its own"):
        DnsRecord(domain="example.com", name="x", value="198.51.100.1")
    with pytest.raises(ValueError, match="needs a 'value'"):
        DnsRecord(domain="example.com", name="x", proxied=False)


def test_a_record_is_proxied_unless_it_says_otherwise():
    """The default is the reason to put a hostname in a CDN at all."""
    assert DnsRecord(domain="example.com", name="www").proxied
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
        control.dns.create_record(DnsRecord(domain="example.com", name="cdn"), "alice")


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


def test_a_dual_stack_hostname_is_one_virtual_host(settings):
    """Two records, one ``server_name``, and nothing to keep in step.

    This used to be enforced: both records had to name the same site, and a
    check refused it when they did not. Neither record carries a policy now, so
    there is nothing for them to disagree about — the hostname resolves once,
    through the zone.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")
    seed_record(control, name="cdn", record_type=RecordType.AAAA)

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
    seed_record(control, name="db", value="198.51.100.11")

    assert control.dns.list_sites() == []


def test_unproxying_requires_the_address_dns_should_answer_with(settings):
    """The one place this parts company with Cloudflare, and why."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn")

    with pytest.raises(ValueError, match="needs a 'value'"):
        control.dns.update_record(
            "example.com", "cdn", RecordType.A, RecordPatch(proxied=False), "alice"
        )

    control.dns.unproxy("example.com", "cdn", RecordType.A, "198.51.100.20", "alice")
    record = control.dns.get_record("example.com", "cdn", RecordType.A)
    assert not record.proxied
    assert record.value == "198.51.100.20"
    assert control.dns.list_sites() == []


def test_proxying_again_clears_the_address(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone(control)
    seed_record(control, name="cdn", value="198.51.100.20")

    record = control.dns.proxy("example.com", "cdn", RecordType.A, "alice")

    assert record.proxied
    assert record.value is None
    assert control.dns.list_sites()[0].server_names == ("cdn.example.com",)


def test_a_proxied_hostname_with_nowhere_to_fetch_from_is_refused(settings):
    """The one contradiction left, and the only one that can still be written."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "alice")
    seed_record(control, name="cdn")

    (error,) = control.dns.validation_errors()
    assert "cdn.example.com" in error
    assert "proxied but nothing says where to fetch it from" in error


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
