"""Placing an HTTP-01 challenge on the edges, without a hook or a subprocess."""

import logging

import pytest
from support.control_plane import ansible_run, host_run

from blitzecdn.capabilities.tls.certificates.adapters import PlaybookChallenges
from blitzecdn.capabilities.tls.certificates.adapters import (
    challenges as challenges_module,
)
from blitzecdn.capabilities.tls.certificates.domain import ChallengeEdges
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ExecutionError


@pytest.fixture(autouse=True)
def _still_clock(monkeypatch):
    """No time passes unless a test says so: the play's timeout is what is
    left of the caller's after edge resolution, in whole seconds."""
    monkeypatch.setattr(challenges_module, "_monotonic", lambda: 0.0)


class _Fleet:
    def __init__(self, run):
        self.run = run
        self.calls = []

    def run_playbook(self, **values):
        self.calls.append(values)
        return self.run


def _challenges(run, edges=None, *, resolve=None):
    fleet = _Fleet(run)
    resolved = edges or ChallengeEdges(hostname="cdn.example.com")

    def challenge_edges(hostname):
        if resolve is not None:
            resolve()
        return resolved

    publisher = PlaybookChallenges(playbooks=fleet, challenge_edges=challenge_edges)
    return publisher, fleet


def test_present_runs_the_challenge_play_with_the_values():
    publisher, fleet = _challenges(ansible_run(host_run("edge-a")))

    publisher.present(
        "cdn.example.com", "safe_token", "safe.validation", timeout_seconds=30
    )

    (call,) = fleet.calls
    assert call["name"] == "acme-challenge"
    assert call["variables"] == {
        "blitzecdn_acme_action": "present",
        "blitzecdn_acme_domain": "cdn.example.com",
        "blitzecdn_acme_token": "safe_token",
        "blitzecdn_acme_validation": "safe.validation",
    }
    # The play is stopped at what the issuance budget has left.
    assert call["timeout_seconds"] == 30
    # The hostname points at no edge we can see, so only the CA knows which it
    # will ask: every edge gets the token.
    assert call["host_limit"] is None


def test_absent_removes_it_again():
    publisher, fleet = _challenges(ansible_run(host_run("edge-a")))

    publisher.absent("cdn.example.com", "safe_token", timeout_seconds=30)

    assert fleet.calls[0]["variables"]["blitzecdn_acme_action"] == "absent"


def test_edges_the_hostname_does_not_resolve_to_are_skipped_and_named(caplog):
    publisher, fleet = _challenges(
        ansible_run(host_run("edge-a")),
        ChallengeEdges(
            hostname="cdn.example.com", targets=("edge-a",), skipped=("edge-b",)
        ),
    )

    with caplog.at_level(logging.INFO):
        publisher.present(
            "cdn.example.com", "safe_token", "safe.validation", timeout_seconds=30
        )

    assert fleet.calls[0]["host_limit"] == "edge-a"
    assert "not on edge-b" in caplog.text


@pytest.mark.parametrize(
    ("domain", "token", "validation"),
    [
        ("bad/domain", "safe_token", "safe.validation"),
        ("cdn.example.com", "bad token", "safe.validation"),
        ("cdn.example.com", "safe_token", "bad validation;"),
    ],
)
def test_a_value_outside_the_acme_alphabet_never_reaches_a_play(
    domain, token, validation
):
    """The CA chooses these, and the play writes them into files on the edges."""
    publisher, fleet = _challenges(ansible_run(host_run("edge-a")))

    with pytest.raises(ExecutionError, match="invalid HTTP-01"):
        publisher.present(domain, token, validation, timeout_seconds=30)

    assert fleet.calls == []


def test_a_failed_play_names_its_log():
    publisher, _fleet = _challenges(
        ansible_run(
            host_run("edge-a", failed=1), status=RunStatus.FAILED, return_code=2
        )
    )

    with pytest.raises(ExecutionError, match="full output:"):
        publisher.absent("cdn.example.com", "safe_token", timeout_seconds=30)


def test_a_play_stopped_at_its_timeout_says_so():
    publisher, fleet = _challenges(
        ansible_run(host_run("edge-a"), status=RunStatus.TIMED_OUT, return_code=254)
    )

    with pytest.raises(ExecutionError, match=r"timed out after 7s .* present"):
        publisher.present(
            "cdn.example.com", "safe_token", "safe.validation", timeout_seconds=7
        )

    assert fleet.calls[0]["timeout_seconds"] == 7


class _SlowDns:
    """Edge resolution that takes ``seconds`` on the clock the publisher reads."""

    def __init__(self, monkeypatch, seconds):
        self.now = 0.0
        self.seconds = seconds
        monkeypatch.setattr(challenges_module, "_monotonic", lambda: self.now)

    def __call__(self):
        self.now += self.seconds


def test_the_play_gets_only_what_edge_resolution_left(monkeypatch):
    """The lookups that pick the edges have DNS timeouts of their own; the
    caller's allowance covers them and the play together."""
    publisher, fleet = _challenges(
        ansible_run(host_run("edge-a")), resolve=_SlowDns(monkeypatch, 12.5)
    )

    publisher.absent("cdn.example.com", "safe_token", timeout_seconds=30)

    assert fleet.calls[0]["timeout_seconds"] == 17


def test_no_play_starts_once_edge_resolution_spent_the_allowance(monkeypatch):
    publisher, fleet = _challenges(
        ansible_run(host_run("edge-a")), resolve=_SlowDns(monkeypatch, 30)
    )

    with pytest.raises(ExecutionError, match="timed out after 30s finding the edges"):
        publisher.absent("cdn.example.com", "safe_token", timeout_seconds=30)

    assert fleet.calls == []
