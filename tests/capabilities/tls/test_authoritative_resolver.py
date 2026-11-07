"""Tests for asking a zone's own nameservers.

The bootstrap resolver and the wire are both replaced: these test the walk
(zone cut, recursion off, CNAMEs, referrals, lame servers), not the network.
"""

from __future__ import annotations

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdatatype
import dns.resolver
import dns.rrset
import pytest

from blitzecdn.capabilities.tls.certificates.adapters import (
    authoritative as authoritative_module,
)
from blitzecdn.capabilities.tls.certificates.adapters.authoritative import (
    AuthoritativeLookupFailed,
    AuthoritativeResolver,
)


class _Answer(list):
    pass


class FakeBootstrap:
    """Answers NS and nameserver-address questions from a table."""

    def __init__(self, records: dict[tuple[str, str], list[str]]) -> None:
        self._records = records
        self.asked: list[tuple[str, str]] = []

    def resolve(self, name, rdtype, *args, **kwargs):
        key = (str(name).rstrip("."), dns.rdatatype.to_text(rdtype))
        self.asked.append(key)
        values = self._records.get(key)
        if values is None:
            raise dns.resolver.NoAnswer
        return [
            dns.rrset.from_text(f"{key[0]}.", 300, "IN", key[1], value)[0]
            for value in values
        ]


ZONES = {
    "www.example.com": "example.com",
    "example.com": "example.com",
    "cdn.example.net": "example.net",
    "deep.sub.example.com": "example.com",
}

BOOTSTRAP = {
    ("example.com", "NS"): ["ns1.example.com.", "ns2.example.com."],
    ("ns1.example.com", "A"): ["192.0.2.1"],
    ("ns2.example.com", "A"): ["192.0.2.2"],
    ("example.net", "NS"): ["ns.example.net."],
    ("ns.example.net", "A"): ["192.0.2.9"],
}


@pytest.fixture
def wire(monkeypatch):
    """Replace the zone-cut lookup and UDP with tables; record every query."""
    sent: list[tuple[str, str, str, bool]] = []
    handlers: dict[str, object] = {}

    def zone_for_name(name, **kwargs):
        return dns.name.from_text(ZONES[str(name).rstrip(".")])

    def udp_with_fallback(query, where, timeout=None, **kwargs):
        question = query.question[0]
        recursion = bool(query.flags & dns.flags.RD)
        sent.append(
            (
                where,
                str(question.name).rstrip("."),
                dns.rdatatype.to_text(question.rdtype),
                recursion,
            )
        )
        handler = handlers[where]
        if isinstance(handler, Exception):
            raise handler
        return handler(query), False

    monkeypatch.setattr(
        authoritative_module.dns.resolver, "zone_for_name", zone_for_name
    )
    monkeypatch.setattr(
        authoritative_module.dns.query, "udp_with_fallback", udp_with_fallback
    )
    return sent, handlers


def _respond(
    *answer: str,
    rcode=dns.rcode.NOERROR,
    authoritative=True,
    authority=(),
    additional=(),
):
    def handler(query):
        response = dns.message.make_response(query)
        response.set_rcode(rcode)
        if authoritative:
            response.flags |= dns.flags.AA
        for text in answer:
            response.answer.append(dns.rrset.from_text(*text.split(" ", 4)))
        for text in authority:
            response.authority.append(dns.rrset.from_text(*text.split(" ", 4)))
        for text in additional:
            response.additional.append(dns.rrset.from_text(*text.split(" ", 4)))
        return response

    return handler


def _resolver(records=BOOTSTRAP):
    return AuthoritativeResolver(timeout=5.0, bootstrap=FakeBootstrap(records))


def test_asks_the_zones_nameserver_with_recursion_off(wire):
    sent, handlers = wire
    handlers["192.0.2.1"] = _respond("www.example.com. 300 IN A 203.0.113.9")
    resolver = _resolver()

    records = resolver.resolve("www.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["203.0.113.9"]
    assert sent == [("192.0.2.1", "www.example.com", "A", False)]
    assert resolver.answered_by("www.example.com.") == "ns1.example.com (192.0.2.1)"


def test_a_lame_nameserver_falls_through_to_the_next(wire):
    sent, handlers = wire
    handlers["192.0.2.1"] = _respond(rcode=dns.rcode.REFUSED)
    handlers["192.0.2.2"] = _respond("www.example.com. 300 IN A 203.0.113.9")
    resolver = _resolver()

    records = resolver.resolve("www.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["203.0.113.9"]
    assert [server for server, *_ in sent] == ["192.0.2.1", "192.0.2.2"]
    assert resolver.answered_by("www.example.com") == "ns2.example.com (192.0.2.2)"


def test_every_server_failing_is_a_dns_error_not_an_empty_answer(wire):
    _, handlers = wire
    handlers["192.0.2.1"] = OSError("unreachable")
    handlers["192.0.2.2"] = _respond(rcode=dns.rcode.SERVFAIL)

    with pytest.raises(AuthoritativeLookupFailed, match="SERVFAIL"):
        _resolver().resolve("www.example.com", dns.rdatatype.A)


def test_nxdomain_and_nodata_raise_as_a_recursive_resolver_would(wire):
    _, handlers = wire
    handlers["192.0.2.1"] = _respond(rcode=dns.rcode.NXDOMAIN)
    with pytest.raises(dns.resolver.NXDOMAIN):
        _resolver().resolve("www.example.com", dns.rdatatype.A)

    handlers["192.0.2.1"] = _respond()
    with pytest.raises(dns.resolver.NoAnswer):
        _resolver().resolve("www.example.com", dns.rdatatype.AAAA)


def test_a_cname_into_another_zone_is_asked_of_that_zone(wire):
    sent, handlers = wire
    handlers["192.0.2.1"] = _respond("www.example.com. 300 IN CNAME cdn.example.net.")
    handlers["192.0.2.9"] = _respond("cdn.example.net. 300 IN A 198.51.100.7")
    resolver = _resolver()

    records = resolver.resolve("www.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["198.51.100.7"]
    assert [(server, name) for server, name, *_ in sent] == [
        ("192.0.2.1", "www.example.com"),
        ("192.0.2.9", "cdn.example.net"),
    ]
    # The report names the server that gave the final answer.
    assert resolver.answered_by("www.example.com") == "ns.example.net (192.0.2.9)"


def test_a_cname_answered_in_the_same_response_needs_no_second_query(wire):
    sent, handlers = wire
    handlers["192.0.2.1"] = _respond(
        "www.example.com. 300 IN CNAME example.com.",
        "example.com. 300 IN A 203.0.113.1",
    )

    records = _resolver().resolve("www.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["203.0.113.1"]
    assert len(sent) == 1


def test_a_referral_is_followed_using_glue(wire):
    """A delegated child zone the bootstrap did not know about."""
    sent, handlers = wire
    handlers["192.0.2.1"] = _respond(
        authoritative=False,
        authority=("sub.example.com. 300 IN NS ns.sub.example.com.",),
        additional=("ns.sub.example.com. 300 IN A 192.0.2.77",),
    )
    handlers["192.0.2.77"] = _respond("deep.sub.example.com. 300 IN A 203.0.113.77")

    records = _resolver().resolve("deep.sub.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["203.0.113.77"]
    assert [server for server, *_ in sent] == ["192.0.2.1", "192.0.2.77"]


def test_a_zone_whose_nameservers_have_no_address_fails(wire):
    records = {("example.com", "NS"): ["ns1.example.com."]}
    with pytest.raises(AuthoritativeLookupFailed, match="no address"):
        _resolver(records).resolve("www.example.com", dns.rdatatype.A)


def test_nameservers_are_looked_up_once_per_zone(wire):
    _, handlers = wire
    handlers["192.0.2.1"] = _respond("www.example.com. 300 IN A 203.0.113.9")
    bootstrap = FakeBootstrap(BOOTSTRAP)
    resolver = AuthoritativeResolver(timeout=5.0, bootstrap=bootstrap)

    resolver.resolve("www.example.com", dns.rdatatype.A)
    resolver.resolve("www.example.com", dns.rdatatype.A)

    assert bootstrap.asked.count(("example.com", "NS")) == 1


def test_an_exhausted_budget_is_a_timeout(wire, monkeypatch):
    _, handlers = wire
    handlers["192.0.2.1"] = _respond("www.example.com. 300 IN A 203.0.113.9")
    with pytest.raises(dns.exception.Timeout):
        AuthoritativeResolver(timeout=0.0, bootstrap=FakeBootstrap(BOOTSTRAP)).resolve(
            "www.example.com", dns.rdatatype.A
        )


def test_a_lame_nameserver_cannot_answer_that_a_name_is_absent(wire):
    """An empty non-authoritative NOERROR is no evidence; ask the next server."""
    _, handlers = wire
    handlers["192.0.2.1"] = _respond(authoritative=False)
    handlers["192.0.2.2"] = _respond("www.example.com. 300 IN A 203.0.113.9")
    resolver = _resolver()

    records = resolver.resolve("www.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["203.0.113.9"]
    assert resolver.answered_by("www.example.com") == "ns2.example.com (192.0.2.2)"


def test_only_lame_nameservers_is_a_failure_not_an_empty_answer(wire):
    _, handlers = wire
    handlers["192.0.2.1"] = _respond(authoritative=False)
    handlers["192.0.2.2"] = _respond(rcode=dns.rcode.NXDOMAIN, authoritative=False)

    with pytest.raises(AuthoritativeLookupFailed, match="lame"):
        _resolver().resolve("www.example.com", dns.rdatatype.A)


def test_a_silent_nameserver_leaves_budget_for_the_next(wire, monkeypatch):
    """Each server gets a share of what is left, not all of it."""
    _, handlers = wire
    clock = [1000.0]
    monkeypatch.setattr(authoritative_module.time, "monotonic", lambda: clock[0])
    timeouts: list[float] = []

    handlers["192.0.2.2"] = _respond("www.example.com. 300 IN A 203.0.113.9")
    original = authoritative_module.dns.query.udp_with_fallback

    def timed(query, where, timeout=None, **kwargs):
        timeouts.append(timeout)
        if where == "192.0.2.1":
            clock[0] += timeout  # the whole share is spent waiting
            raise dns.exception.Timeout
        return original(query, where, timeout=timeout, **kwargs)

    monkeypatch.setattr(authoritative_module.dns.query, "udp_with_fallback", timed)

    records = _resolver().resolve("www.example.com", dns.rdatatype.A)

    assert [record.address for record in records] == ["203.0.113.9"]
    assert timeouts == [2.5, 2.5]
