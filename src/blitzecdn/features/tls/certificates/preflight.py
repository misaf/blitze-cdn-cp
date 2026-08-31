"""Check the outside world before asking a CA for a certificate.

Issuance goes through HTTP-01: the CA resolves the hostname in public DNS and
fetches ``/.well-known/acme-challenge/<token>`` over port 80. Everything that
has to be true for that to work lives outside the control plane — public DNS
must point at an edge, the vhost must already exist on that edge, and CAA must
permit our CA. None of it was checked before, so the three most common customer
mistakes all surfaced the same way: an opaque certbot error, minutes later,
after a rate-limited request had already been spent.

This looks at each of those from the controller and reports what it found. The
controller is not the CA's vantage point, so a pass is evidence rather than
proof — the same caveat ``origins.py`` carries. A blocking failure, though, is
a condition under which validation cannot succeed, and is worth refusing on.

Nothing here mutates anything. The only hosts contacted are ones an operator
already declared: the site's own hostnames, its origin, and the edges in the
inventory.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from collections.abc import Iterable, Iterator

import dns.exception
import dns.rdatatype
import dns.resolver

from blitzecdn.core.config import Settings
from blitzecdn.features.edges import OriginProbeAdapter
from blitzecdn.features.edges.ports import EdgeStore, OriginProbe
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.tls.certificates.domain import (
    TTL_CUTOVER_ADVISORY_SECONDS,
    PreflightCheck,
    PreflightReport,
    PreflightSeverity,
)

#: CAA property tags we understand. ``issue`` governs ordinary issuance,
#: ``issuewild`` wildcard issuance; RFC 8659 says a wildcard order consults
#: ``issuewild`` and falls back to ``issue`` only when no ``issuewild`` is
#: present in the RRset.
_CAA_ISSUE = "issue"
_CAA_ISSUEWILD = "issuewild"


class CertificatePreflight:
    """Answers "would HTTP-01 work for this site right now?"."""

    def __init__(
        self,
        settings: Settings,
        edges: EdgeStore,
        origin_probe: OriginProbe | None = None,
    ) -> None:
        self._settings = settings
        self._edges = edges
        self._origin_probe = origin_probe or OriginProbeAdapter(settings)

    def _resolver(self) -> dns.resolver.Resolver:
        """A resolver honouring the configured timeout and, if set, servers."""
        resolver = dns.resolver.Resolver()
        timeout = float(self._settings.preflight_dns_timeout_seconds)
        resolver.lifetime = timeout
        resolver.timeout = timeout
        if self._settings.preflight_dns_servers:
            resolver.nameservers = list(self._settings.preflight_dns_servers)
        return resolver

    def _address_resolver(self) -> dns.resolver.Resolver | None:
        """``None`` keeps A/AAAA on ``getaddrinfo``, the long-standing default.

        Switching every deployment to dnspython would change which answers
        count — ``/etc/hosts`` entries and search domains would stop applying —
        so the explicit-server path is opt-in.
        """
        if not self._settings.preflight_dns_servers:
            return None
        return self._resolver()

    def _resolver_note(self) -> str:
        servers = self._settings.preflight_dns_servers
        return f" via {_join_names(servers)}" if servers else ""

    def check(
        self, site: CdnSite, *, deployed: bool, record_ttl: int | None = None
    ) -> PreflightReport:
        """Run every check for one site.

        ``deployed`` and ``record_ttl`` come from the caller because they are
        facts about stored state, which this adapter deliberately cannot read.
        """
        checks = [
            self._check_dns(site),
            self._check_caa(site),
            self._check_deployed(site, deployed=deployed),
            self._check_origin(site),
        ]
        if record_ttl is not None:
            checks.append(self._check_ttl(record_ttl))
        return PreflightReport(site=site.name, checks=tuple(checks))

    # ------------------------------------------------------------------
    # Does public DNS point at an edge?
    # ------------------------------------------------------------------

    def _check_dns(self, site: CdnSite) -> PreflightCheck:
        resolvable = [name for name in site.server_names if not name.startswith("*.")]
        if not resolvable:
            # A wildcard site cannot be resolved and cannot use HTTP-01 anyway;
            # `CertbotIssuer` refuses it separately with a better message.
            return _passed(
                "dns",
                "wildcard-only site; nothing to resolve (HTTP-01 cannot issue "
                "for it — upload a certificate instead)",
            )
        edges = self._edge_addresses()
        if not edges:
            return _failed(
                "dns",
                PreflightSeverity.BLOCKING,
                "no edge address could be resolved from the inventory, so there "
                "is nothing to compare the hostname against; add an edge first",
            )
        resolver = self._address_resolver()
        note = self._resolver_note()
        problems: list[str] = []
        for name in resolvable:
            addresses = _resolve(name, resolver)
            if not addresses:
                problems.append(f"{name} does not resolve{note}")
            elif not addresses & edges:
                problems.append(
                    f"{name} resolves{note} to {_join(addresses)}, which is not "
                    f"an edge address ({_join(edges)})"
                )
        if problems:
            return _failed("dns", PreflightSeverity.BLOCKING, "; ".join(problems))
        return _passed("dns", f"{_join_names(resolvable)} resolve{note} to an edge")

    def _edge_addresses(self) -> set[str]:
        """Every address the recorded edges answer on publicly.

        ``effective_public_addresses`` rather than the connection host: this
        asks what the *world* reaches, and on a NAT'd edge the address the
        controller connects to is not it. A public address may be a name rather
        than an address, so each one is resolved. A single unresolvable edge is
        skipped rather than failing the check — the question here is whether the
        customer's hostname lands on one of our edges, and an edge we cannot
        resolve is a separate problem that ``validate`` and a deploy both
        surface on their own.
        """
        addresses: set[str] = set()
        try:
            edges = self._edges.list_edges()
        except Exception:  # noqa: BLE001 -- an unreadable fleet is reported by
            # `validate` and by every deploy; preflight must not be the thing
            # that turns it into a stack trace.
            return addresses
        resolver = self._address_resolver()
        for edge in edges:
            for address in edge.effective_public_addresses:
                addresses |= _resolve(address, resolver)
        return addresses

    # ------------------------------------------------------------------
    # Does CAA permit our CA?
    # ------------------------------------------------------------------

    def _check_caa(self, site: CdnSite) -> PreflightCheck:
        issuer = self._settings.acme_ca_domain
        for name in site.server_names:
            wildcard = name.startswith("*.")
            base = name[2:] if wildcard else name
            try:
                records = self._closest_caa(base)
            except dns.exception.DNSException as exc:
                # Advisory, not blocking: a flaky or slow resolver here is not
                # evidence of a hostile CAA record, and refusing issuance over
                # it would make a transient failure look like a policy.
                return _failed(
                    "caa",
                    PreflightSeverity.ADVISORY,
                    f"CAA for {base} could not be checked ({type(exc).__name__}); "
                    "issuance may still be refused by the CA",
                )
            if records is None:
                continue
            owner, tags = records
            allowed = _permitted_issuers(tags, wildcard=wildcard)
            if allowed is None:
                continue
            if issuer not in allowed:
                return _failed(
                    "caa",
                    PreflightSeverity.BLOCKING,
                    f"CAA on {owner} does not permit {issuer} "
                    f"(allows: {_join(allowed) or 'no issuer at all'}); "
                    f"add a CAA record for {issuer} or remove the restriction",
                )
        return _passed("caa", f"no CAA record forbids {issuer}")

    def _closest_caa(self, name: str) -> tuple[str, list[tuple[str, str]]] | None:
        """The CAA RRset at the closest ancestor that has one, or ``None``.

        RFC 8659: a CAA RRset on ``a.b.example.com`` is authoritative for it and
        an absent one delegates the question to the parent, so the walk stops at
        the first name that answers rather than merging what it finds.
        """
        resolver = self._resolver()
        for candidate in _ancestors(name):
            try:
                answer = resolver.resolve(candidate, dns.rdatatype.CAA)
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            tags = [
                (
                    record.tag.decode("ascii", "replace").lower(),
                    record.value.decode("ascii", "replace").strip(),
                )
                for record in answer
            ]
            if tags:
                return candidate, tags
        return None

    # ------------------------------------------------------------------
    # State the caller had to look up for us
    # ------------------------------------------------------------------

    @staticmethod
    def _check_deployed(site: CdnSite, *, deployed: bool) -> PreflightCheck:
        if deployed:
            return _passed("deployed", "the vhost is live on the edges")
        return _failed(
            "deployed",
            PreflightSeverity.BLOCKING,
            f"{site.name} is not in the last successful deployment, so no edge "
            "serves its ACME challenge path yet; run 'blitzecdn deploy' first",
        )

    @staticmethod
    def _check_ttl(ttl: int) -> PreflightCheck:
        if ttl <= TTL_CUTOVER_ADVISORY_SECONDS:
            return _passed("ttl", f"record TTL is {ttl}s")
        return _failed(
            "ttl",
            PreflightSeverity.ADVISORY,
            f"record TTL is {ttl}s; resolvers that cached the previous address "
            "may keep using it for that long, which can fail validation during "
            "a cutover. Lower it before moving the hostname",
        )

    def _check_origin(self, site: CdnSite) -> PreflightCheck:
        """Probe the origin. Advisory: a cert is issuable while the origin is down.

        Included anyway because the customer-visible result of issuing against a
        dead origin is a padlock over a 502, which reads as our failure.
        """
        result = self._origin_probe.check(site)
        if result.ok:
            return _passed("origin", f"{result.origin} answered as expected")
        return _failed(
            "origin",
            PreflightSeverity.ADVISORY,
            f"{result.origin}: {result.detail}",
        )


def check_resolver(settings: Settings) -> PreflightCheck:
    """Detect a resolver that invents answers for names that cannot exist.

    A forwarding proxy that claims every hostname looks perfectly healthy —
    browsing works, packages install, the CA is reachable — right up until
    something compares a resolved address against an expected one. Then every
    DNS check fails for a reason that points at the zone rather than at the
    resolver, which is a long way to travel for the wrong conclusion.

    ``.invalid`` is reserved by RFC 2606 and can never be delegated, so a
    correct resolver answers NXDOMAIN. Anything that returns an address for a
    random name under it is answering questions it cannot know.
    """
    servers = settings.preflight_dns_servers
    where = f" ({_join_names(servers)})" if servers else " (host resolver)"
    resolver = dns.resolver.Resolver()
    timeout = float(settings.preflight_dns_timeout_seconds)
    resolver.lifetime = timeout
    resolver.timeout = timeout
    if servers:
        resolver.nameservers = list(servers)
    probe = f"blitzecdn-resolver-probe-{secrets.token_hex(8)}.invalid"
    try:
        answer = resolver.resolve(probe, dns.rdatatype.A)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return _passed("resolver", f"resolver{where} rejects names that cannot exist")
    except dns.exception.DNSException:
        # No answer at all is not evidence of dishonesty, and preflight already
        # degrades gracefully when a resolver is slow or unreachable.
        return _passed("resolver", f"resolver{where} did not answer the probe")
    addresses = _join({str(record.address) for record in answer})
    return _failed(
        "resolver",
        PreflightSeverity.BLOCKING,
        f"resolver{where} answered {addresses} for a reserved .invalid name, so "
        "it invents addresses instead of resolving. Every DNS check will "
        "compare against fiction. Point this host at a resolver that returns "
        "real answers, or set BLITZE_PREFLIGHT_DNS_SERVERS",
    )


def _passed(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(
        name=name, passed=True, severity=PreflightSeverity.ADVISORY, detail=detail
    )


def _failed(name: str, severity: PreflightSeverity, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, passed=False, severity=severity, detail=detail)


def _permitted_issuers(
    tags: Iterable[tuple[str, str]], *, wildcard: bool
) -> set[str] | None:
    """Issuer domains a CAA RRset allows, or ``None`` if it does not constrain us.

    ``None`` and the empty set are different answers: no relevant tag in the
    RRset means CAA is silent about this kind of issuance and the parent's view
    does not apply either, whereas ``issue ";"`` is an explicit refusal of
    every issuer and must block.
    """
    relevant = _CAA_ISSUEWILD if wildcard else _CAA_ISSUE
    values = [value for tag, value in tags if tag == relevant]
    if wildcard and not values:
        # No issuewild in the RRset, so wildcard issuance falls back to issue.
        values = [value for tag, value in tags if tag == _CAA_ISSUE]
    if not values:
        return None
    allowed: set[str] = set()
    for value in values:
        # "letsencrypt.org; validationmethods=http-01" — parameters after the
        # first semicolon are the CA's business, not ours. A bare ";" is the
        # refuse-everything form and contributes no issuer.
        domain = value.split(";", 1)[0].strip().lower().rstrip(".")
        if domain:
            allowed.add(domain)
    return allowed


def _ancestors(name: str) -> Iterator[str]:
    """``a.b.example.com`` then ``b.example.com`` then ``example.com``.

    Stops before the single-label TLD: no CAA record we could act on lives
    there, and querying it adds a round trip per issuance for nothing.
    """
    labels = name.rstrip(".").split(".")
    for index in range(len(labels) - 1):
        yield ".".join(labels[index:])


def _resolve(host: str, resolver: dns.resolver.Resolver | None = None) -> set[str]:
    """Every A/AAAA address ``host`` answers with, normalized for comparison.

    An ``ansible_host`` that is already a literal address is returned as-is
    rather than sent to the resolver. Addresses are normalized through
    ``ip_address`` so two spellings of one IPv6 address compare equal.

    Without an explicit resolver this goes through ``getaddrinfo``, which is
    what the host itself would do — ``/etc/hosts``, ``nsswitch.conf`` and all.
    With one, the query is sent to those servers instead, because the question
    preflight is really asking is what the CA will see from the public
    internet, not what this machine happens to believe.
    """
    candidate = host.strip().rstrip(".")
    if not candidate:
        return set()
    try:
        return {str(ipaddress.ip_address(candidate))}
    except ValueError:
        pass
    if resolver is not None:
        return _resolve_with(candidate, resolver)
    try:
        infos = socket.getaddrinfo(candidate, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return set()
    addresses: set[str] = set()
    for info in infos:
        try:
            addresses.add(str(ipaddress.ip_address(info[4][0])))
        except ValueError:
            continue
    return addresses


def _resolve_with(candidate: str, resolver: dns.resolver.Resolver) -> set[str]:
    """A and AAAA from an explicit resolver, ignoring the host's own view."""
    addresses: set[str] = set()
    for rdatatype in (dns.rdatatype.A, dns.rdatatype.AAAA):
        try:
            answer = resolver.resolve(candidate, rdatatype)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException:
            # A resolver that cannot answer at all is indistinguishable here
            # from a name that does not exist. The caller reports "does not
            # resolve", which is the right conclusion either way.
            continue
        for record in answer:
            try:
                addresses.add(str(ipaddress.ip_address(record.address)))
            except ValueError:
                continue
    return addresses


def _join(values: Iterable[str]) -> str:
    return ", ".join(sorted(values))


def _join_names(values: Iterable[str]) -> str:
    return ", ".join(values)
