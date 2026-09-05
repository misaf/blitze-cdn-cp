"""The one check in `doctor` that leaves the machine.

`check_resolver` had no test. `tests/entrypoints/test_cli.py` covers `doctor`
by monkeypatching this function with a canned `ResolverCheck`, which asserts
what the command does with an answer and nothing about how the answer is
reached — so the probe itself, the three ways a resolver can respond to it, and
the wording an operator is shown were unverified.

The probe is the point: a resolver that invents addresses for names that cannot
exist makes every other preflight check wrong while staying invisible to all of
them. So what is asserted here is that a *refusal* is the passing case, and an
*answer* is the failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import dns.exception
import dns.resolver
import pytest

from blitzecdn.capabilities.diagnostics.cli import check_resolver


class FakeResolver:
    """Records how it was configured, and answers however the test says.

    Constructed by `check_resolver` rather than injected, so the class is what
    a test substitutes and the instance is what it inspects afterwards.
    """

    last: Any = None

    def __init__(self) -> None:
        self.nameservers: list[str] = []
        self.lifetime: float | None = None
        self.timeout: float | None = None
        self.asked: list[str] = []
        type(self).last = self

    def resolve(self, name: str, rdatatype: Any) -> Any:
        self.asked.append(name)
        return self._answer(name, rdatatype)

    @staticmethod
    def _answer(_name: str, _rdatatype: Any) -> Any:
        raise NotImplementedError


def _resolver(answer) -> type[FakeResolver]:
    return type("PatchedResolver", (FakeResolver,), {"_answer": staticmethod(answer)})


@pytest.fixture
def settings() -> SimpleNamespace:
    return SimpleNamespace(preflight_dns_servers=(), preflight_dns_timeout_seconds=5)


def _patch(monkeypatch: pytest.MonkeyPatch, answer) -> type[FakeResolver]:
    resolver = _resolver(answer)
    monkeypatch.setattr(dns.resolver, "Resolver", resolver)
    return resolver


def test_a_resolver_that_rejects_a_name_that_cannot_exist_passes(monkeypatch, settings):
    def nxdomain(_name, _rdatatype):
        raise dns.resolver.NXDOMAIN

    _patch(monkeypatch, nxdomain)

    result = check_resolver(settings)

    assert result.passed
    assert "rejects names that cannot exist" in result.detail


def test_an_empty_answer_passes_for_the_same_reason(monkeypatch, settings):
    """`NoAnswer` is a refusal too: the resolver invented nothing."""

    def no_answer(_name, _rdatatype):
        raise dns.resolver.NoAnswer

    _patch(monkeypatch, no_answer)

    assert check_resolver(settings).passed


def test_a_resolver_that_does_not_answer_at_all_passes(monkeypatch, settings):
    """A timeout is not evidence of the defect this looks for.

    `doctor` is a local readiness report, and failing it because a probe timed
    out would make an offline machine look misconfigured. The distinct wording
    is what tells an operator the probe was inconclusive rather than clean.
    """

    def timeout(_name, _rdatatype):
        raise dns.exception.Timeout

    _patch(monkeypatch, timeout)

    result = check_resolver(settings)

    assert result.passed
    assert "did not answer the probe" in result.detail


def test_a_resolver_that_answers_a_reserved_name_fails_and_names_the_addresses(
    monkeypatch, settings
):
    """The defect itself: an address for a name that cannot have one."""
    _patch(
        monkeypatch,
        lambda _name, _rdatatype: [
            SimpleNamespace(address="10.0.0.2"),
            SimpleNamespace(address="10.0.0.1"),
        ],
    )

    result = check_resolver(settings)

    assert not result.passed
    assert "10.0.0.1, 10.0.0.2" in result.detail
    assert "invents addresses" in result.detail


def test_the_probe_asks_for_a_reserved_name_it_has_never_asked_before(
    monkeypatch, settings
):
    """`.invalid` cannot be delegated, and the label is fresh each run.

    A fixed name would be answered from a cache — the resolver's or an
    intermediary's — so a second run could pass on a stale negative answer from
    a resolver that has since started inventing addresses.
    """

    def nxdomain(_name, _rdatatype):
        raise dns.resolver.NXDOMAIN

    resolver = _patch(monkeypatch, nxdomain)

    check_resolver(settings)
    first = resolver.last.asked
    check_resolver(settings)
    second = resolver.last.asked

    assert first[0].endswith(".invalid")
    assert first != second


def test_the_probe_is_bounded_by_the_configured_timeout(monkeypatch, settings):
    """Both budgets, not just one.

    `timeout` bounds a single server and `lifetime` the whole query; setting
    only the first lets a resolver list with several entries take a multiple of
    the configured budget, and `doctor` is a command an operator runs while
    waiting for it.
    """

    def nxdomain(_name, _rdatatype):
        raise dns.resolver.NXDOMAIN

    settings.preflight_dns_timeout_seconds = 2
    resolver = _patch(monkeypatch, nxdomain)

    check_resolver(settings)

    assert resolver.last.timeout == 2.0
    assert resolver.last.lifetime == 2.0


def test_configured_servers_are_probed_and_named_in_the_detail(monkeypatch, settings):
    """Which resolver answered is half the report.

    An operator reading "resolver invents addresses" needs to know whether that
    was the host's resolver or the one they configured for preflight, because
    the two are fixed in different places.
    """

    def nxdomain(_name, _rdatatype):
        raise dns.resolver.NXDOMAIN

    settings.preflight_dns_servers = ("192.0.2.1", "192.0.2.2")
    resolver = _patch(monkeypatch, nxdomain)

    result = check_resolver(settings)

    assert resolver.last.nameservers == ["192.0.2.1", "192.0.2.2"]
    assert "(192.0.2.1, 192.0.2.2)" in result.detail


def test_the_host_resolver_is_named_when_none_is_configured(monkeypatch, settings):
    def nxdomain(_name, _rdatatype):
        raise dns.resolver.NXDOMAIN

    resolver = _patch(monkeypatch, nxdomain)

    result = check_resolver(settings)

    assert resolver.last.nameservers == []
    assert "(host resolver)" in result.detail
