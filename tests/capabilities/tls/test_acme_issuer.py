"""ACME issuance against a scripted CA.

The protocol objects are the library's own (`acme.messages`, `acme.challenges`),
so key authorizations, tokens and identifiers are computed exactly as against a
real CA; only the HTTP transport is replaced. The Pebble stage of
`tests/container-install.sh` covers the transport against a CA that validates.
"""

from __future__ import annotations

import stat
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests
from acme import challenges, messages
from acme import errors as acme_errors
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from josepy import b64
from support.policy import site_from_settings

from blitzecdn.capabilities.tls.certificates.adapters import acme as acme_module
from blitzecdn.capabilities.tls.certificates.adapters.acme import AcmeIssuer
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError

_CHAIN = "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----\n"


class _Publisher:
    def __init__(self, *, fail_on: str | None = None, fail_absent=False):
        self.events: list[tuple[str, ...]] = []
        self.fail_on = fail_on
        self.fail_absent = fail_absent

    def present(self, domain, token, validation):
        if domain == self.fail_on:
            raise ExecutionError(f"could not present for {domain}")
        self.events.append(("present", domain, token, validation))

    def absent(self, domain, token):
        self.events.append(("absent", domain, token))
        if self.fail_absent:
            raise ExecutionError(f"edge unreachable removing {domain}")


def _authorization(domain: str, *, status=messages.STATUS_PENDING):
    challenge = messages.ChallengeBody(
        chall=challenges.HTTP01(token=domain.encode().ljust(32, b"t")),
        uri=f"https://ca.test/chall/{domain}",
        status=messages.STATUS_PENDING,
    )
    return messages.AuthorizationResource(
        uri=f"https://ca.test/authz/{domain}",
        body=messages.Authorization(
            identifier=messages.Identifier(typ=messages.IDENTIFIER_FQDN, value=domain),
            challenges=[challenge],
            status=status,
        ),
    )


class _Ca:
    """One scripted CA: what it answers, and what it was asked."""

    def __init__(self, *, existing_account=False, finalize=None, valid=()):
        self.existing_account = existing_account
        self.finalize = finalize
        self.valid = set(valid)
        self.accounts: list[object] = []
        self.keys: list[object] = []
        self.csr: bytes | None = None
        self.answered: list[str] = []
        self.verify: object = True
        self.directory_url: str | None = None

    def install(self, monkeypatch):
        ca = self

        class Network:
            def __init__(self, key, *, alg, user_agent):
                ca.keys.append(key)
                self.key = key
                self.account = None
                self.verify_ssl = True
                ca.network = self

        class Client:
            def __init__(self, directory, net):
                self.net = net

            @staticmethod
            def get_directory(url, net):
                ca.directory_url = url
                ca.verify = net.verify_ssl
                return object()

            def new_account(self, registration):
                ca.accounts.append(registration)
                if ca.existing_account:
                    raise acme_errors.ConflictError("https://ca.test/acct/1")
                return messages.RegistrationResource(
                    uri="https://ca.test/acct/1", body=messages.Registration()
                )

            def new_order(self, csr_pem):
                ca.csr = csr_pem
                request = x509.load_pem_x509_csr(csr_pem)
                names = request.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value.get_values_for_type(x509.DNSName)
                return messages.OrderResource(
                    uri="https://ca.test/order/1",
                    body=messages.Order(),
                    authorizations=[
                        _authorization(
                            name,
                            status=messages.STATUS_VALID
                            if name in ca.valid
                            else messages.STATUS_PENDING,
                        )
                        for name in names
                    ],
                    csr_pem=csr_pem,
                )

            def answer_challenge(self, challenge, response):
                ca.answered.append(challenge.uri)

            def poll_and_finalize(self, order, deadline=None):
                if ca.finalize is not None:
                    ca.finalize(order)
                return order.update(fullchain_pem=_CHAIN)

        monkeypatch.setattr(
            acme_module,
            "client",
            SimpleNamespace(ClientNetwork=Network, ClientV2=Client),
        )
        return self


def _site(*names):
    return site_from_settings(
        {
            "name": "example-com",
            "server_names": list(names),
            "origin_host": "origin.example.com",
        }
    )


def _issuer(tmp_path, publisher, **options):
    return AcmeIssuer(
        directory_url="https://ca.test/directory",
        account_dir=tmp_path / "acme",
        challenges=publisher,
        timeout_seconds=60,
        **options,
    )


def test_every_pending_name_is_presented_answered_and_removed(monkeypatch, tmp_path):
    ca = _Ca().install(monkeypatch)
    publisher = _Publisher()

    chain, key_pem = _issuer(tmp_path, publisher).issue(
        _site("cdn.example.com", "www.example.com"), "ops@example.com"
    )

    assert chain == _CHAIN.encode()
    assert ca.directory_url == "https://ca.test/directory"
    presented = [event for event in publisher.events if event[0] == "present"]
    assert [event[1] for event in presented] == ["cdn.example.com", "www.example.com"]
    # The value served is the key authorization: token "." account thumbprint.
    thumbprint = ca.network.key.thumbprint()
    for _action, _domain, token, validation in presented:
        assert validation.startswith(token + ".")
        assert validation.endswith(b64.b64encode(thumbprint).decode())
    assert len(ca.answered) == 2
    assert [event[0] for event in publisher.events[-2:]] == ["absent", "absent"]
    # The returned key is the one the CSR was signed with.
    request = x509.load_pem_x509_csr(ca.csr)
    key = serialization.load_pem_private_key(key_pem, password=None)
    assert key.public_key().public_numbers() == request.public_key().public_numbers()


def test_the_account_key_is_created_once_private_and_reused(monkeypatch, tmp_path):
    ca = _Ca(existing_account=True).install(monkeypatch)
    issuer = _issuer(tmp_path, _Publisher())

    issuer.issue(_site("cdn.example.com"), "ops@example.com")
    issuer.issue(_site("cdn.example.com"), "ops@example.com")

    path = tmp_path / "acme/account-key.pem"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ca.keys[0].thumbprint() == ca.keys[1].thumbprint()
    # An existing registration is found by its key, not created again.
    assert ca.network.account.uri == "https://ca.test/acct/1"
    assert ca.accounts[0].terms_of_service_agreed is True


def test_an_authorization_the_ca_already_holds_is_not_presented(monkeypatch, tmp_path):
    _Ca(valid={"cdn.example.com"}).install(monkeypatch)
    publisher = _Publisher()

    _issuer(tmp_path, publisher).issue(
        _site("cdn.example.com", "www.example.com"), "ops@example.com"
    )

    assert {event[1] for event in publisher.events} == {"www.example.com"}


def test_a_wildcard_is_refused_before_the_ca_is_asked(monkeypatch, tmp_path):
    ca = _Ca().install(monkeypatch)

    with pytest.raises(ConfigurationError, match="wildcard"):
        _issuer(tmp_path, _Publisher()).issue(_site("*.example.com"), "ops@example.com")

    assert ca.directory_url is None


def test_a_failed_validation_reports_the_cas_reason_and_cleans_up(
    monkeypatch, tmp_path
):
    def refuse(order):
        failed = order.authorizations[0]
        body = failed.body.challenges[0].update(
            error=messages.Error.with_code("unauthorized", detail="404 from edge")
        )
        raise acme_errors.ValidationError(
            [failed.update(body=failed.body.update(challenges=(body,)))]
        )

    _Ca(finalize=refuse).install(monkeypatch)
    publisher = _Publisher()

    with pytest.raises(ExecutionError, match=r"cdn\.example\.com: .*404 from edge"):
        _issuer(tmp_path, publisher).issue(_site("cdn.example.com"), "ops@example.com")

    assert publisher.events[-1][0] == "absent"


def test_a_timeout_is_reported_and_cleans_up(monkeypatch, tmp_path):
    def wait_forever(order):
        raise acme_errors.TimeoutError()

    _Ca(finalize=wait_forever).install(monkeypatch)
    publisher = _Publisher()

    with pytest.raises(ExecutionError, match="timed out"):
        _issuer(tmp_path, publisher).issue(_site("cdn.example.com"), "ops@example.com")

    assert publisher.events[-1][0] == "absent"


def test_a_challenge_that_cannot_be_placed_removes_the_ones_that_were(
    monkeypatch, tmp_path
):
    ca = _Ca().install(monkeypatch)
    publisher = _Publisher(fail_on="www.example.com")

    with pytest.raises(ExecutionError, match="could not present"):
        _issuer(tmp_path, publisher).issue(
            _site("cdn.example.com", "www.example.com"), "ops@example.com"
        )

    assert [event[:2] for event in publisher.events] == [
        ("present", "cdn.example.com"),
        ("absent", "cdn.example.com"),
    ]
    assert ca.answered == []


def test_a_private_ca_bundle_reaches_the_request_requests_sends(monkeypatch, tmp_path):
    """Through the library's real network layer, which sets `verify` per request
    and so overrides anything set on its session."""
    sent = []

    class Refused(requests.ConnectionError):
        pass

    def capture(self, method, url, **options):
        sent.append(options.get("verify"))
        raise Refused("stopped after the first request")

    monkeypatch.setattr(requests.Session, "request", capture)
    bundle = tmp_path / "ca.pem"

    with pytest.raises(ExecutionError, match="certificate request failed"):
        _issuer(tmp_path, _Publisher(), ca_bundle=bundle).issue(
            _site("cdn.example.com"), "ops@example.com"
        )

    assert sent == [str(bundle)]


def test_a_failed_cleanup_keeps_the_issued_certificate(monkeypatch, tmp_path):
    """The CA has spent an issuance on it; an unreachable edge is no reason to
    throw it away and spend another."""
    _Ca().install(monkeypatch)
    publisher = _Publisher(fail_absent=True)

    chain, _key = _issuer(tmp_path, publisher).issue(
        _site("cdn.example.com", "www.example.com"), "ops@example.com"
    )

    assert chain == _CHAIN.encode()
    # Each removal is attempted, not just the first.
    assert [event[:2] for event in publisher.events if event[0] == "absent"] == [
        ("absent", "cdn.example.com"),
        ("absent", "www.example.com"),
    ]


def test_a_failed_cleanup_does_not_replace_the_original_error(monkeypatch, tmp_path):
    def wait_forever(order):
        raise acme_errors.TimeoutError()

    _Ca(finalize=wait_forever).install(monkeypatch)

    with pytest.raises(ExecutionError, match="timed out"):
        _issuer(tmp_path, _Publisher(fail_absent=True)).issue(
            _site("cdn.example.com"), "ops@example.com"
        )


def test_a_spent_budget_stops_before_the_next_challenge(monkeypatch, tmp_path):
    """Placing a challenge runs a playbook; a slow one cannot stretch the
    deployment lock past the issuance budget by placing the rest."""
    _Ca().install(monkeypatch)

    class SlowPublisher(_Publisher):
        def present(self, domain, token, validation):
            super().present(domain, token, validation)
            clock[0] += timedelta(seconds=120)

    clock = [datetime(2026, 1, 1)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(acme_module, "datetime", Clock)
    publisher = SlowPublisher()

    with pytest.raises(ExecutionError, match="timed out"):
        _issuer(tmp_path, publisher).issue(
            _site("cdn.example.com", "www.example.com"), "ops@example.com"
        )

    assert [event[:2] for event in publisher.events] == [
        ("present", "cdn.example.com"),
        ("absent", "cdn.example.com"),
    ]
