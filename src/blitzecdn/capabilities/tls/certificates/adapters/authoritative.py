"""Ask a zone's own nameservers, the way a certificate authority does.

A recursive resolver answers from its cache, and one inside a private network
may answer from a split-horizon view. The CA asks the public internet, which
ends at the zone's authoritative servers, so preflight asks them too.

A bootstrap recursive resolver finds the zone cut and its nameservers' names
and addresses; the record itself comes from those servers with recursion off.
Delegation data changes rarely and is the same in every view that matters, so
trusting the bootstrap for it is a smaller risk than trusting it for answers.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.rrset

#: A CNAME chain or referral path longer than this is a loop or a misconfiguration.
_MAX_HOPS = 8
#: Addresses tried per step: enough to survive a lame server, bounded so one
#: unreachable zone cannot spend the whole budget.
_MAX_SERVERS = 4


class AuthoritativeLookupFailed(dns.exception.DNSException):
    """No authoritative server gave a usable answer."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)  # type: ignore[no-untyped-call]


class AuthoritativeResolver:
    """Resolves records by querying each zone's authoritative nameservers.

    Quacks like :class:`dns.resolver.Resolver` for what preflight needs:
    ``resolve(name, rdtype)`` returns iterable rdata, or raises ``NXDOMAIN`` /
    ``NoAnswer`` / another :class:`dns.exception.DNSException`. One instance
    serves one preflight run, and remembers which server answered each name.
    """

    def __init__(self, *, timeout: float, bootstrap: dns.resolver.Resolver) -> None:
        self._timeout = timeout
        self._bootstrap = bootstrap
        self._zones: dict[dns.name.Name, list[tuple[str, str]]] = {}
        self._answered_by: dict[str, str] = {}

    def answered_by(self, name: str) -> str | None:
        """The server whose answer decided ``name``, as ``host (address)``."""
        return self._answered_by.get(_key(name))

    def resolve(self, name: str, rdtype: dns.rdatatype.RdataType) -> list[Any]:
        """The rdata answering ``name``; ``Any``, as dnspython's own answers are."""
        deadline = time.monotonic() + self._timeout
        original = _key(name)
        qname = dns.name.from_text(name)
        zone: dns.name.Name | None = None
        servers: list[tuple[str, str]] | None = None
        for _ in range(_MAX_HOPS):
            if servers is None:
                zone, servers = self._nameservers_for(qname, deadline)
            response, server = self._ask(qname, rdtype, zone, servers, deadline)
            self._answered_by[original] = f"{server[0]} ({server[1]})"
            records, target = _follow(response, qname, rdtype)
            if records:
                return records
            if target is not None:
                # The chain left this response; the target may live elsewhere.
                qname, servers = target, None
                continue
            if response.rcode() == dns.rcode.NXDOMAIN:
                raise dns.resolver.NXDOMAIN(qnames=[qname])  # type: ignore[no-untyped-call]
            referral = _referral(response, qname, zone)
            if referral is not None:
                zone = referral.name
                servers = self._referral_servers(referral, response, deadline)
                continue
            raise dns.resolver.NoAnswer
        raise AuthoritativeLookupFailed(f"{name}: more than {_MAX_HOPS} hops")

    # ------------------------------------------------------------------

    def _nameservers_for(
        self, qname: dns.name.Name, deadline: float
    ) -> tuple[dns.name.Name, list[tuple[str, str]]]:
        zone = dns.resolver.zone_for_name(
            qname, resolver=self._bootstrap, lifetime=_remaining(deadline)
        )
        cached = self._zones.get(zone)
        if cached is not None:
            return zone, cached
        answer = self._bootstrap.resolve(
            zone, dns.rdatatype.NS, lifetime=_remaining(deadline)
        )
        hosts = sorted(str(record.target) for record in answer)
        servers = self._addresses(hosts, {}, deadline)
        if not servers:
            raise AuthoritativeLookupFailed(f"no address for any nameserver of {zone}")
        self._zones[zone] = servers
        return zone, servers

    def _referral_servers(
        self,
        referral: dns.rrset.RRset,
        response: dns.message.Message,
        deadline: float,
    ) -> list[tuple[str, str]]:
        cached = self._zones.get(referral.name)
        if cached is not None:
            return cached
        glue: dict[str, list[str]] = {}
        for rrset in response.additional:
            if rrset.rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
                glue.setdefault(str(rrset.name), []).extend(
                    record.address for record in rrset
                )
        hosts = sorted(str(record.target) for record in referral)
        servers = self._addresses(hosts, glue, deadline)
        if not servers:
            raise AuthoritativeLookupFailed(
                f"no address for any nameserver of {referral.name}"
            )
        self._zones[referral.name] = servers
        return servers

    def _addresses(
        self, hosts: Iterable[str], glue: dict[str, list[str]], deadline: float
    ) -> list[tuple[str, str]]:
        """``(host, address)`` pairs, IPv4 first: the controller may lack IPv6."""
        found: list[tuple[str, str]] = []
        for rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
            for host in hosts:
                if len(found) >= _MAX_SERVERS:
                    return found
                if host in glue:
                    v6 = rdtype == dns.rdatatype.AAAA
                    addresses = [a for a in glue[host] if (":" in a) == v6]
                else:
                    try:
                        answer = self._bootstrap.resolve(
                            host, rdtype, lifetime=_remaining(deadline)
                        )
                    except dns.exception.Timeout:
                        raise
                    except dns.exception.DNSException:
                        continue
                    addresses = [record.address for record in answer]
                found.extend((host.rstrip("."), address) for address in addresses)
        return found[:_MAX_SERVERS]

    def _ask(
        self,
        qname: dns.name.Name,
        rdtype: dns.rdatatype.RdataType,
        zone: dns.name.Name | None,
        servers: list[tuple[str, str]],
        deadline: float,
    ) -> tuple[dns.message.Message, tuple[str, str]]:
        """The first usable response: authoritative, or a referral further down.

        Each server gets an equal share of what is left, so one that drops
        packets cannot spend the budget the healthy ones need. A response that
        is neither authoritative nor a referral is lame (a server listed for a
        zone it does not serve) and says nothing about whether the name exists.
        """
        query = dns.message.make_query(qname, rdtype)
        query.flags &= ~dns.flags.RD
        errors: list[str] = []
        for index, server in enumerate(servers):
            share = _remaining(deadline) / (len(servers) - index)
            try:
                response, _ = dns.query.udp_with_fallback(
                    query, server[1], timeout=share
                )
            except dns.exception.Timeout:
                if time.monotonic() >= deadline:
                    raise
                errors.append(f"{server[0]}: timeout")
                continue
            except (OSError, dns.exception.DNSException) as exc:
                errors.append(f"{server[0]}: {type(exc).__name__}")
                continue
            if response.rcode() not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
                errors.append(f"{server[0]}: {dns.rcode.to_text(response.rcode())}")
                continue
            if response.flags & dns.flags.AA:
                return response, server
            if _referral(response, qname, zone) is not None:
                return response, server
            errors.append(f"{server[0]}: lame (not authoritative for {qname})")
        raise AuthoritativeLookupFailed(f"{qname}: {'; '.join(errors) or 'no servers'}")


def _follow(
    response: dns.message.Message,
    qname: dns.name.Name,
    rdtype: dns.rdatatype.RdataType,
) -> tuple[list[Any], dns.name.Name | None]:
    """The records for ``qname``, following CNAMEs within the response.

    Returns ``(records, None)`` on an answer, or ``([], target)`` when a chain
    leaves the response and must be asked of the target's own zone.
    """
    name = qname
    for _ in range(_MAX_HOPS):
        rrset = _find(response.answer, name, rdtype)
        if rrset:
            return list(rrset), None
        if rdtype == dns.rdatatype.CNAME:
            break
        cname = _find(response.answer, name, dns.rdatatype.CNAME)
        if not cname:
            break
        name = cname[0].target
    return [], (name if name != qname else None)


def _find(
    section: Iterable[dns.rrset.RRset],
    name: dns.name.Name,
    rdtype: dns.rdatatype.RdataType,
) -> dns.rrset.RRset | None:
    """Scan rather than index: a section is a handful of RRsets."""
    for rrset in section:
        if (
            rrset.name == name
            and rrset.rdtype == rdtype
            and rrset.rdclass == dns.rdataclass.IN
        ):
            return rrset
    return None


def _referral(
    response: dns.message.Message,
    qname: dns.name.Name,
    zone: dns.name.Name | None,
) -> dns.rrset.RRset | None:
    """A delegation further down than ``zone``, toward ``qname``."""
    if response.flags & dns.flags.AA or response.answer:
        return None
    for rrset in response.authority:
        if rrset.rdtype != dns.rdatatype.NS:
            continue
        deeper = zone is None or (rrset.name != zone and rrset.name.is_subdomain(zone))
        if deeper and qname.is_subdomain(rrset.name):
            return rrset
    return None


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise dns.exception.Timeout
    return left


def _key(name: str | dns.name.Name) -> str:
    return str(name).rstrip(".").lower()
