"""Tests for certificate preflight.

``_resolve`` and the CAA lookup are replaced, so nothing touches the network:
these test the decision, not the finding.
"""

from __future__ import annotations

from typing import ClassVar

import dns.exception
import dns.resolver
import pytest
from support.control_plane import FakeEdgeStore

from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.origins.domain import OriginCheck
from blitzecdn.capabilities.tls.certificates.adapters import (
    preflight as preflight_module,
)
from blitzecdn.capabilities.tls.certificates.adapters.authoritative import (
    AuthoritativeResolver,
)
from blitzecdn.capabilities.tls.certificates.adapters.preflight import (
    CertificatePreflight,
    _ancestors,
    _permitted_issuers,
)
from blitzecdn.capabilities.tls.certificates.domain import PreflightSeverity
from blitzecdn.capabilities.tls.contract import TlsPolicy
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.core.domain.site import CdnSite


class FakeOriginProbe:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    def to_probe(self, site: CdnSite) -> dict[str, object]:
        return {"name": site.name, "origin_host": site.origin_host}

    def check(self, site: CdnSite) -> OriginCheck:
        return OriginCheck(
            site=site.name,
            origin=f"{site.origin_host}:443",
            scheme=site.policy[TlsPolicy].canonical_origin_scheme,
            ssl_mode=site.policy[TlsPolicy].ssl_mode,
            reachable=self.ok,
            detail=None if self.ok else "no answer within 5s",
        )


@pytest.fixture
def config() -> CertificateConfig:
    return CertificateConfig()


@pytest.fixture
def site() -> CdnSite:
    return CdnSite(
        name="example-com",
        server_names=("cdn.example.com",),
        origin_host="198.51.100.10",
    )


@pytest.fixture
def build(config, monkeypatch):
    """A preflight whose resolver and CAA lookup answer from a dict."""

    def make(
        *,
        ca_domain: str = "letsencrypt.org",
        addresses: dict[str, set[str]] | None = None,
        caa: tuple[str, list[tuple[str, str]]] | None = None,
        caa_error: Exception | None = None,
        edges: list[Edge] | None = None,
        origin_ok: bool = True,
    ) -> CertificatePreflight:
        resolved = addresses or {}
        monkeypatch.setattr(
            preflight_module,
            "_resolve",
            lambda host, resolver=None: resolved.get(host, set()),
        )

        def closest_caa(self, name):
            if caa_error is not None:
                raise caa_error
            return caa

        monkeypatch.setattr(CertificatePreflight, "_closest_caa", closest_caa)

        return CertificatePreflight(
            config.model_copy(update={"ca_domain": ca_domain}),
            FakeEdgeStore(edges),
            origin_probe=FakeOriginProbe(ok=origin_ok),
        )

    return make


def _check(report, name):
    return next(check for check in report.checks if check.name == name)


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_ancestors_walks_up_but_stops_before_the_tld():
    assert list(_ancestors("a.b.example.com")) == [
        "a.b.example.com",
        "b.example.com",
        "example.com",
    ]


def test_a_caa_rrset_with_no_relevant_tag_does_not_constrain_us():
    """An RRset of only iodef says nothing about who may issue."""
    assert _permitted_issuers([("iodef", "mailto:a@b.com")], wildcard=False) is None


def test_issue_semicolon_refuses_every_issuer():
    """Distinct from `None`: an explicit refusal must block, not fall through."""
    assert _permitted_issuers([("issue", ";")], wildcard=False) == set()


def test_issuer_parameters_are_not_part_of_the_identity():
    assert _permitted_issuers(
        [("issue", "letsencrypt.org; validationmethods=http-01")], wildcard=False
    ) == {"letsencrypt.org"}


def test_a_wildcard_falls_back_to_issue_when_no_issuewild_exists():
    tags = [("issue", "letsencrypt.org")]
    assert _permitted_issuers(tags, wildcard=True) == {"letsencrypt.org"}


def test_issuewild_overrides_issue_for_a_wildcard():
    tags = [("issue", "letsencrypt.org"), ("issuewild", "other-ca.example")]
    assert _permitted_issuers(tags, wildcard=True) == {"other-ca.example"}
    assert _permitted_issuers(tags, wildcard=False) == {"letsencrypt.org"}


# ----------------------------------------------------------------------
# Does DNS point at an edge?
# ----------------------------------------------------------------------


def test_a_hostname_on_an_edge_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        }
    ).check(site, deployed=True)
    assert _check(report, "dns").passed
    assert report.ok


def test_public_edge_addresses_are_distinct_from_the_ssh_host(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "localhost": {"127.0.0.1"},
            "203.0.113.5": {"203.0.113.5"},
        },
        edges=[
            Edge(
                name="edge1",
                host="localhost",
                public_addresses=("203.0.113.5",),
            )
        ],
    ).check(site, deployed=True)
    assert _check(report, "dns").passed
    assert report.ok


def test_a_hostname_pointed_somewhere_else_blocks_and_names_both_sides(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"198.51.100.99"},
            "edge1.example.net": {"203.0.113.5"},
        }
    ).check(site, deployed=True)
    failure = _check(report, "dns")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    # The operator has to be able to see what to change without a second tool.
    assert "198.51.100.99" in failure.detail
    assert "203.0.113.5" in failure.detail
    assert not report.ok


def test_a_stale_aaaa_beside_an_edge_a_record_blocks_and_names_it(build, site):
    """The CA prefers IPv6, so the old server would answer the challenge."""
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5", "2001:db8::5"},
            "edge1.example.net": {"203.0.113.5"},
        }
    ).check(site, deployed=True)
    failure = _check(report, "dns")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    assert (
        "also resolves via its authoritative nameservers to 2001:db8::5, "
        "which is not an edge"
    ) in failure.detail
    assert "remove that record" in failure.detail
    assert not report.ok


def test_extra_a_records_beside_an_edge_block_and_are_all_named(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5", "198.51.100.7", "198.51.100.8"},
            "edge1.example.net": {"203.0.113.5"},
        }
    ).check(site, deployed=True)
    failure = _check(report, "dns")
    assert not failure.passed
    assert "198.51.100.7, 198.51.100.8, which are not edges" in failure.detail
    # The edge address is fine and must not be listed as something to remove.
    assert "203.0.113.5" not in failure.detail
    assert not report.ok


def test_a_hostname_spread_across_several_edges_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5", "2001:db8::1"},
            "edge1.example.net": {"203.0.113.5", "2001:db8::1"},
        }
    ).check(site, deployed=True)
    assert _check(report, "dns").passed


def test_an_unresolvable_hostname_blocks(build, site):
    report = build(addresses={"edge1.example.net": {"203.0.113.5"}}).check(
        site, deployed=True
    )
    assert "does not resolve" in _check(report, "dns").detail
    assert not report.ok


def test_a_fleet_with_no_usable_edge_blocks_rather_than_passing(build, site):
    """With nothing to compare against, the check cannot pass vacuously."""
    report = build(addresses={"cdn.example.com": {"203.0.113.5"}}, edges=[]).check(
        site, deployed=True
    )
    assert not _check(report, "dns").passed
    assert "no edge address" in _check(report, "dns").detail


def test_an_unreadable_fleet_does_not_raise(config, monkeypatch, site):
    class ExplodingEdgeStore:
        def list_edges(self):
            raise OSError("the edges table is unreadable")

    monkeypatch.setattr(
        preflight_module, "_resolve", lambda host, resolver=None: {"203.0.113.5"}
    )
    monkeypatch.setattr(
        CertificatePreflight,
        "_closest_caa",
        lambda self, name: None,
    )
    report = CertificatePreflight(
        config,
        ExplodingEdgeStore(),
        origin_probe=FakeOriginProbe(),
    ).check(site, deployed=True)
    assert not _check(report, "dns").passed


# ----------------------------------------------------------------------
# Which edges an HTTP-01 challenge has to reach
# ----------------------------------------------------------------------


def _edges(*names_and_addresses):
    return [
        Edge(name=name, host=f"{name}.example.net", public_addresses=(address,))
        for name, address in names_and_addresses
    ]


def test_a_challenge_skips_edges_the_hostname_does_not_resolve_to(build):
    edges = _edges(("edge1", "203.0.113.5"), ("edge2", "203.0.113.6"))
    result = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "203.0.113.5": {"203.0.113.5"},
            "203.0.113.6": {"203.0.113.6"},
        },
        edges=edges,
    ).challenge_edges("cdn.example.com")
    assert result.targets == ("edge1",)
    assert result.skipped == ("edge2",)


def test_a_challenge_keeps_every_edge_the_hostname_resolves_to(build):
    edges = _edges(("edge1", "203.0.113.5"), ("edge2", "2001:db8::6"))
    result = build(
        addresses={
            "cdn.example.com": {"203.0.113.5", "2001:db8::6"},
            "203.0.113.5": {"203.0.113.5"},
            "2001:db8::6": {"2001:db8::6"},
        },
        edges=edges,
    ).challenge_edges("cdn.example.com")
    assert result.targets == ("edge1", "edge2")
    assert result.skipped == ()


def test_a_challenge_keeps_an_edge_whose_own_address_does_not_resolve(build):
    """Nothing shows the hostname is not on it, so it may still be asked."""
    edges = [
        *_edges(("edge1", "203.0.113.5"), ("edge2", "203.0.113.6")),
        Edge(name="edge3", host="edge3.example.net", public_addresses=("gone.test",)),
    ]
    result = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "203.0.113.5": {"203.0.113.5"},
            "203.0.113.6": {"203.0.113.6"},
        },
        edges=edges,
    ).challenge_edges("cdn.example.com")
    assert result.targets == ("edge1", "edge3")
    assert result.skipped == ("edge2",)


@pytest.mark.parametrize("resolved", [set(), {"198.51.100.99"}])
def test_a_challenge_for_a_hostname_on_no_known_edge_targets_the_fleet(build, resolved):
    """Preflight was overridden: only the CA knows which address it will ask."""
    edges = _edges(("edge1", "203.0.113.5"), ("edge2", "203.0.113.6"))
    result = build(
        addresses={
            "cdn.example.com": resolved,
            "203.0.113.5": {"203.0.113.5"},
            "203.0.113.6": {"203.0.113.6"},
        },
        edges=edges,
    ).challenge_edges("cdn.example.com")
    assert result.targets == ()
    assert result.skipped == ()


def test_a_wildcard_site_is_not_resolved(build):
    wildcard = CdnSite(
        name="wildcard-example-com",
        server_names=("*.example.com",),
        origin_host="198.51.100.10",
    )
    report = build().check(wildcard, deployed=True)
    assert _check(report, "dns").passed
    assert "wildcard" in _check(report, "dns").detail


# ----------------------------------------------------------------------
# CAA
# ----------------------------------------------------------------------


def test_no_caa_record_anywhere_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=None,
    ).check(site, deployed=True)
    assert _check(report, "caa").passed


def test_caa_naming_another_ca_blocks_and_says_which(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=("example.com", [("issue", "other-ca.example")]),
    ).check(site, deployed=True)
    failure = _check(report, "caa")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    assert "other-ca.example" in failure.detail
    assert "letsencrypt.org" in failure.detail


def test_caa_permitting_our_ca_passes(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=("example.com", [("issue", "letsencrypt.org")]),
    ).check(site, deployed=True)
    assert _check(report, "caa").passed
    assert report.ok


def test_a_broken_resolver_is_an_advisory_not_a_block(build, site):
    """A flaky resolver is not evidence of a hostile CAA record."""
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa_error=dns.exception.Timeout(),
    ).check(site, deployed=True)
    failure = _check(report, "caa")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.ADVISORY
    assert report.ok


def test_a_configured_ca_is_what_caa_is_checked_against(build, site):
    """Pointing certbot at another ACME server must move the CAA check with it."""
    checker = build(
        ca_domain="other-ca.example",
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        caa=("example.com", [("issue", "other-ca.example")]),
    )
    assert _check(checker.check(site, deployed=True), "caa").passed


# ----------------------------------------------------------------------
# Deployment, origin, TTL
# ----------------------------------------------------------------------


def test_an_undeployed_site_blocks_with_the_fix_named(build, site):
    report = build().check(site, deployed=False)
    failure = _check(report, "deployed")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.BLOCKING
    assert "blitzecdn deploy" in failure.detail


def test_a_dead_origin_is_advisory_because_a_cert_is_still_issuable(build, site):
    report = build(
        addresses={
            "cdn.example.com": {"203.0.113.5"},
            "edge1.example.net": {"203.0.113.5"},
        },
        origin_ok=False,
    ).check(site, deployed=True)
    failure = _check(report, "origin")
    assert not failure.passed
    assert failure.severity is PreflightSeverity.ADVISORY
    assert report.ok


def test_a_high_ttl_is_advisory_and_a_normal_one_passes(build, site):
    high = build().check(site, deployed=True, record_ttl=86400)
    assert not _check(high, "ttl").passed
    assert _check(high, "ttl").severity is PreflightSeverity.ADVISORY
    assert _check(build().check(site, deployed=True, record_ttl=300), "ttl").passed


def test_ttl_is_omitted_when_the_caller_does_not_supply_one(build, site):
    report = build().check(site, deployed=True)
    assert not [check for check in report.checks if check.name == "ttl"]


def test_summary_lists_only_blocking_failures(build, site):
    report = build(
        addresses={"edge1.example.net": {"203.0.113.5"}},
        origin_ok=False,
    ).check(site, deployed=False, record_ttl=86400)
    summary = report.summary()
    assert "dns" in summary
    assert "deployed" in summary
    assert "ttl" not in summary
    assert {check.name for check in report.advisories} == {"ttl"}


def test_a_dns_block_skips_caa_and_the_origin_probe(config, monkeypatch, site):
    """An unpointed site must not cost a CAA walk or a connection to its origin."""

    class RefusingOriginProbe(FakeOriginProbe):
        def check(self, site: CdnSite) -> OriginCheck:
            raise AssertionError("a DNS block must not probe the origin")

    def refuse_caa(self, name):
        raise AssertionError("a DNS block must not walk CAA")

    monkeypatch.setattr(
        preflight_module,
        "_resolve",
        lambda host, resolver=None: (
            set() if host == "www.example.com" else {"198.51.100.1"}
        ),
    )
    monkeypatch.setattr(CertificatePreflight, "_closest_caa", refuse_caa)
    monkeypatch.setattr(
        CertificatePreflight, "_edge_addresses", lambda self: {"203.0.113.5"}
    )
    report = CertificatePreflight(
        config,
        FakeEdgeStore(),
        origin_probe=RefusingOriginProbe(),
    ).check(site, deployed=False, record_ttl=300)

    assert [check.name for check in report.checks] == ["dns", "deployed", "ttl"]
    assert {check.name for check in report.blocking_failures} == {"dns", "deployed"}


# ----------------------------------------------------------------------
# The two seams that reach the network
# ----------------------------------------------------------------------


def test_resolve_returns_a_literal_address_without_asking_the_resolver(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a literal address must not be sent to the resolver")

    monkeypatch.setattr(preflight_module.socket, "getaddrinfo", explode)
    assert preflight_module._resolve("203.0.113.5") == {"203.0.113.5"}
    assert preflight_module._resolve("  203.0.113.5.  ") == {"203.0.113.5"}


def test_resolve_normalizes_so_two_spellings_of_one_address_compare_equal(monkeypatch):
    monkeypatch.setattr(
        preflight_module.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, "", ("2001:0DB8:0000::1", 0, 0, 0))],
    )
    assert preflight_module._resolve("edge.example.net") == {"2001:db8::1"}


def test_resolve_treats_a_lookup_failure_as_no_addresses(monkeypatch):
    monkeypatch.setattr(
        preflight_module.socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(OSError("NXDOMAIN")),
    )
    assert preflight_module._resolve("absent.example") == set()
    assert preflight_module._resolve("") == set()


class _FakeCaaRecord:
    def __init__(self, tag: bytes, value: bytes) -> None:
        self.tag = tag
        self.value = value


def _host(config):
    """Recursive lookups through the (patched) host resolver."""
    return config.model_copy(update={"preflight_dns_source": "host"})


def test_closest_caa_stops_at_the_first_ancestor_that_answers(config, monkeypatch):
    """RFC 8659: the nearest RRset decides; the parent's is not merged in."""
    asked: list[str] = []

    class FakeResolver:
        lifetime = 0.0
        timeout = 0.0

        def resolve(self, name, _rdtype):
            asked.append(name)
            if name == "cdn.example.com":
                raise dns.resolver.NoAnswer
            if name == "example.com":
                return [_FakeCaaRecord(b"ISSUE", b"letsencrypt.org")]
            raise AssertionError("the walk should have stopped at example.com")

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    found = CertificatePreflight(_host(config), FakeEdgeStore())._closest_caa(
        "cdn.example.com"
    )

    assert found == ("example.com", [("issue", "letsencrypt.org")])
    assert asked == ["cdn.example.com", "example.com"]


def test_closest_caa_returns_none_when_no_ancestor_has_a_record(config, monkeypatch):
    class FakeResolver:
        lifetime = 0.0
        timeout = 0.0

        def resolve(self, name, _rdtype):
            raise dns.resolver.NXDOMAIN

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    assert (
        CertificatePreflight(_host(config), FakeEdgeStore())._closest_caa(
            "cdn.example.com"
        )
        is None
    )


# --- explicit preflight resolvers -------------------------------------------


class _Address:
    def __init__(self, address: str) -> None:
        self.address = address


class RecordingResolver:
    """Captures what it was asked and answers from a fixed table."""

    instances: ClassVar[list[RecordingResolver]] = []

    def __init__(self) -> None:
        self.nameservers: list[str] = []
        self.lifetime = 0.0
        self.timeout = 0.0
        self.asked: list[tuple[str, object]] = []
        RecordingResolver.instances.append(self)

    def resolve(self, name, rdatatype):
        self.asked.append((name, rdatatype))
        if rdatatype is dns.rdatatype.A and name == "www.example.com":
            return [_Address("203.0.113.9")]
        raise dns.resolver.NoAnswer


@pytest.fixture
def recording_resolver(monkeypatch):
    RecordingResolver.instances = []
    monkeypatch.setattr(dns.resolver, "Resolver", RecordingResolver)
    return RecordingResolver


def test_configured_servers_are_used_instead_of_the_host_resolver(
    config, monkeypatch, recording_resolver
):
    """The controller's own view of DNS must not decide what the CA will see."""

    def explode(*args, **kwargs):
        raise AssertionError("getaddrinfo must not be used when servers are set")

    monkeypatch.setattr(preflight_module.socket, "getaddrinfo", explode)
    configured = config.model_copy(update={"preflight_dns_servers": "1.1.1.1,1.0.0.1"})
    addresses = preflight_module._resolve(
        "www.example.com",
        CertificatePreflight(configured, FakeEdgeStore())._address_resolver(),
    )
    assert addresses == {"203.0.113.9"}
    assert recording_resolver.instances[-1].nameservers == ["1.1.1.1", "1.0.0.1"]


def test_by_default_addresses_come_from_authoritative_nameservers(config):
    """The controller's cache and split-horizon view must not stand in for the CA's."""
    preflight = CertificatePreflight(config, FakeEdgeStore())
    assert isinstance(preflight._address_resolver(), AuthoritativeResolver)
    assert isinstance(preflight._resolver(), AuthoritativeResolver)


def test_the_host_resolver_is_an_explicit_opt_in(config):
    """``host`` keeps ``getaddrinfo``, so /etc/hosts applies when asked for."""
    assert (
        CertificatePreflight(_host(config), FakeEdgeStore())._address_resolver() is None
    )


def test_the_dns_check_names_the_authoritative_server_that_answered(
    config, monkeypatch
):
    def answered(self, name):
        return "ns1.example.net (192.0.2.53)"

    monkeypatch.setattr(AuthoritativeResolver, "answered_by", answered)
    monkeypatch.setattr(
        preflight_module, "_resolve", lambda host, resolver=None: {"198.51.100.1"}
    )
    monkeypatch.setattr(CertificatePreflight, "_closest_caa", lambda self, name: None)

    report = CertificatePreflight(
        config,
        FakeEdgeStore(),
        origin_probe=FakeOriginProbe(),
    ).check(
        CdnSite(
            name="site",
            server_names=("www.example.com",),
            origin_host="origin.example.com",
        ),
        deployed=True,
    )
    detail = next(check.detail for check in report.checks if check.name == "dns")
    assert detail == (
        "www.example.com resolves via authoritative nameserver "
        "ns1.example.net (192.0.2.53) only to edges"
    )


def test_the_dns_check_says_when_the_host_resolver_answered(config, monkeypatch):
    monkeypatch.setattr(
        preflight_module,
        "_resolve",
        lambda host, resolver=None: (
            set() if host == "www.example.com" else {"198.51.100.1"}
        ),
    )
    report = CertificatePreflight(
        _host(config),
        FakeEdgeStore(),
        origin_probe=FakeOriginProbe(),
    ).check(
        CdnSite(
            name="site",
            server_names=("www.example.com",),
            origin_host="origin.example.com",
        ),
        deployed=True,
    )
    detail = next(check.detail for check in report.checks if check.name == "dns")
    assert "via the host resolver" in detail


def test_the_dns_check_names_the_resolver_it_asked(config, monkeypatch, build):
    configured = config.model_copy(update={"preflight_dns_servers": "9.9.9.9"})
    monkeypatch.setattr(
        preflight_module,
        "_resolve",
        lambda host, resolver=None: {"198.51.100.1"},
    )
    monkeypatch.setattr(CertificatePreflight, "_closest_caa", lambda self, name: None)

    report = CertificatePreflight(
        configured,
        FakeEdgeStore(),
        origin_probe=FakeOriginProbe(),
    ).check(
        CdnSite(
            name="site",
            server_names=("www.example.com",),
            origin_host="origin.example.com",
        ),
        deployed=True,
    )
    detail = next(check.detail for check in report.checks if check.name == "dns")
    assert "via 9.9.9.9" in detail


# --- resolver honesty probe --------------------------------------------------


def test_a_resolver_that_rejects_reserved_names_passes(config, recording_resolver):
    check = preflight_module.check_resolver(config)
    assert check.passed
    probed = recording_resolver.instances[-1].asked[0][0]
    assert probed.endswith(".invalid"), "probe must use a name that cannot be delegated"


def test_a_resolver_that_answers_a_reserved_name_blocks(config, monkeypatch):
    """The exact pathology of a transparent proxy claiming every hostname."""

    class LyingResolver:
        def __init__(self) -> None:
            self.nameservers: list[str] = []
            self.lifetime = 0.0
            self.timeout = 0.0

        def resolve(self, name, rdatatype):
            return [_Address("172.16.200.20")]

    monkeypatch.setattr(dns.resolver, "Resolver", LyingResolver)
    check = preflight_module.check_resolver(config)
    assert not check.passed
    assert check.severity is PreflightSeverity.BLOCKING
    assert "172.16.200.20" in check.detail
    assert "invents addresses" in check.detail


def test_an_unreachable_resolver_is_not_reported_as_dishonest(config, monkeypatch):
    class DeadResolver:
        def __init__(self) -> None:
            self.nameservers: list[str] = []
            self.lifetime = 0.0
            self.timeout = 0.0

        def resolve(self, name, rdatatype):
            raise dns.exception.Timeout

    monkeypatch.setattr(dns.resolver, "Resolver", DeadResolver)
    assert preflight_module.check_resolver(config).passed
