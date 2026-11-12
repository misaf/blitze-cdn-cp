"""ACME issuance against a scripted CA.

The protocol objects are the library's own (`acme.messages`, `acme.challenges`),
so key authorizations, tokens and identifiers are computed exactly as against a
real CA; only the HTTP transport is replaced. The Pebble stage of
`tests/integration/container-install.sh` covers the transport against a CA that
validates.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml
from acme import challenges, messages
from acme import errors as acme_errors
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from josepy import b64
from support.paths import CORE_ANSIBLE
from support.policy import site_from_settings

from blitzecdn.capabilities.tls.certificates.adapters import acme as acme_module
from blitzecdn.capabilities.tls.certificates.adapters.acme import AcmeIssuer
from blitzecdn.capabilities.tls.settings import acme_account_dir, certificate_dir
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError
from blitzecdn.core.runtime.lock import DeploymentLock

_CHAIN = "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----\n"


class _Publisher:
    def __init__(self, *, fail_on: str | None = None, fail_absent=False):
        self.events: list[tuple[str, ...]] = []
        self.timeouts: list[tuple[str, int]] = []
        self.fail_on = fail_on
        self.fail_absent = fail_absent

    def present(self, domain, token, validation, *, timeout_seconds):
        self.timeouts.append(("present", timeout_seconds))
        if domain == self.fail_on:
            raise ExecutionError(f"could not present for {domain}")
        self.events.append(("present", domain, token, validation))

    def absent(self, domain, token, *, timeout_seconds):
        self.timeouts.append(("absent", timeout_seconds))
        self.events.append(("absent", domain, token))
        if self.fail_absent:
            raise ExecutionError(f"edge unreachable removing {domain}")


class _Clock:
    """The monotonic clock the budget reads, moved only by the test."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        monkeypatch.setattr(acme_module, "_monotonic", lambda: self.now)

    def elapsed(self) -> float:
        return self.now - 1000.0


class _Transport:
    """The HTTP layer under the issuer's session: answers at once, except that
    a request sent during ``stall_at`` never answers, and is cut off by the
    timeout the session gave it, as `requests` would."""

    def __init__(self, monkeypatch, clock, *, stall_at=None):
        self.clock = clock
        self.stall_at = stall_at
        self.step = None
        self.sent: list[tuple[str, float]] = []
        transport = self

        def request(session, method, url, **options):
            transport.sent.append((transport.step, options["timeout"]))
            if transport.step == transport.stall_at:
                clock.now += options["timeout"]
                raise requests.ReadTimeout(f"no answer from {url}")
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(requests.Session, "request", request)

    def send(self, net, step):
        """What one library call does on the wire: a request on its session."""
        self.step = step
        net.session.request("POST", f"https://ca.test/{step}")


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

    def __init__(
        self, *, existing_account=False, finalize=None, valid=(), transport=None
    ):
        self.transport = transport
        self.existing_account = existing_account
        self.finalize = finalize
        self.valid = set(valid)
        self.accounts: list[object] = []
        self.keys: list[object] = []
        self.csr: bytes | None = None
        self.answered: list[str] = []
        self.verify: object = True
        self.directory_url: str | None = None
        self.deadline = None

    def wire(self, net, step):
        if self.transport is not None:
            self.transport.send(net, step)

    def install(self, monkeypatch):
        ca = self

        class Network:
            def __init__(self, key, *, alg, user_agent):
                ca.keys.append(key)
                self.key = key
                self.account = None
                self.verify_ssl = True
                self.session = requests.Session()
                ca.network = self

        class Client:
            def __init__(self, directory, net):
                self.net = net

            @staticmethod
            def get_directory(url, net):
                ca.directory_url = url
                ca.verify = net.verify_ssl
                ca.wire(net, "directory discovery")
                return object()

            def new_account(self, registration):
                ca.wire(self.net, "account registration")
                ca.accounts.append(registration)
                if ca.existing_account:
                    raise acme_errors.ConflictError("https://ca.test/acct/1")
                return messages.RegistrationResource(
                    uri="https://ca.test/acct/1", body=messages.Registration()
                )

            def new_order(self, csr_pem):
                ca.wire(self.net, "new order")
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
                ca.wire(self.net, "answering")
                ca.answered.append(challenge.uri)

            def poll_and_finalize(self, order, deadline=None):
                ca.deadline = deadline
                ca.wire(self.net, "polling and finalization")
                if ca.finalize is not None:
                    ca.finalize(order)
                return order.update(fullchain_pem=_CHAIN)

        monkeypatch.setattr(
            acme_module,
            "client",
            SimpleNamespace(
                ClientNetwork=Network, ClientV2=Client, DEFAULT_NETWORK_TIMEOUT=45
            ),
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


def _issuer(tmp_path, publisher, *, timeout_seconds=60, **options):
    return AcmeIssuer(
        directory_url="https://ca.test/directory",
        account_dir=tmp_path / "acme",
        challenges=publisher,
        timeout_seconds=timeout_seconds,
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

    # A play that failed may have reached some edges, so it is removed too.
    assert [event[:2] for event in publisher.events] == [
        ("present", "cdn.example.com"),
        ("absent", "cdn.example.com"),
        ("absent", "www.example.com"),
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
    clock = _Clock(monkeypatch)

    class SlowPublisher(_Publisher):
        def present(self, domain, token, validation, *, timeout_seconds):
            super().present(domain, token, validation, timeout_seconds=timeout_seconds)
            clock.now += 120

    publisher = SlowPublisher()

    with pytest.raises(ExecutionError, match="timed out during placing the challenge"):
        _issuer(tmp_path, publisher).issue(
            _site("cdn.example.com", "www.example.com"), "ops@example.com"
        )

    assert [event[:2] for event in publisher.events] == [
        ("present", "cdn.example.com"),
        ("absent", "cdn.example.com"),
    ]


def test_each_play_gets_what_is_left_of_the_budget(monkeypatch, tmp_path):
    """60 s in all: 15 s are cleanup's, 45 s are issuance's, and each placement
    is stopped at what the ones before it left."""
    _Ca().install(monkeypatch)
    clock = _Clock(monkeypatch)

    class SlowPublisher(_Publisher):
        def present(self, domain, token, validation, *, timeout_seconds):
            super().present(domain, token, validation, timeout_seconds=timeout_seconds)
            clock.now += 10

    publisher = SlowPublisher()
    _issuer(tmp_path, publisher).issue(
        _site("cdn.example.com", "www.example.com"), "ops@example.com"
    )

    assert publisher.timeouts == [
        ("present", 45),
        ("present", 35),
        ("absent", 15),
        ("absent", 15),
    ]


@pytest.mark.parametrize(
    "step",
    [
        "directory discovery",
        "account registration",
        "new order",
        "answering",
        "polling and finalization",
    ],
)
def test_a_stalled_ca_is_cut_off_within_the_budget(monkeypatch, tmp_path, step):
    """However the CA stalls, issuance ends inside the timeout the deployment
    lock's lease assumes, and says where it stopped."""
    clock = _Clock(monkeypatch)
    transport = _Transport(monkeypatch, clock, stall_at=step)
    _Ca(transport=transport).install(monkeypatch)
    publisher = _Publisher()
    issuer = _issuer(tmp_path, publisher, timeout_seconds=600)

    with pytest.raises(ExecutionError, match=f"timed out during {step}"):
        issuer.issue(_site("cdn.example.com"), "ops@example.com")

    assert clock.elapsed() <= 600
    # Every request was capped at the library's own default and at the budget.
    assert all(0 < timeout <= 45 for _step, timeout in transport.sent)
    # Nothing was sent after the stall.
    assert transport.sent[-1][0] == step
    if step in {"answering", "polling and finalization"}:
        assert publisher.events[-1][0] == "absent"


def test_a_request_is_never_given_more_than_the_budget_left(monkeypatch, tmp_path):
    clock = _Clock(monkeypatch)
    transport = _Transport(monkeypatch, clock)
    ca = _Ca(transport=transport).install(monkeypatch)

    class SlowPublisher(_Publisher):
        def present(self, domain, token, validation, *, timeout_seconds):
            super().present(domain, token, validation, timeout_seconds=timeout_seconds)
            clock.now += 40

    # 60 s in all, 45 s of it issuance's; placing took 40 of those.
    _issuer(tmp_path, SlowPublisher()).issue(
        _site("cdn.example.com"), "ops@example.com"
    )

    assert dict(transport.sent)["answering"] == pytest.approx(5)
    # The library's polling is handed the same budget as a wall-clock deadline.
    assert ca.deadline is not None


def test_a_spent_budget_sends_nothing_more(monkeypatch, tmp_path):
    clock = _Clock(monkeypatch)
    transport = _Transport(monkeypatch, clock)
    _Ca(transport=transport).install(monkeypatch)

    class SpendingPublisher(_Publisher):
        def present(self, domain, token, validation, *, timeout_seconds):
            super().present(domain, token, validation, timeout_seconds=timeout_seconds)
            clock.now += timeout_seconds

    with pytest.raises(ExecutionError, match="timed out during answering"):
        _issuer(tmp_path, SpendingPublisher()).issue(
            _site("cdn.example.com"), "ops@example.com"
        )

    assert [step for step, _timeout in transport.sent] == [
        "directory discovery",
        "account registration",
        "new order",
    ]


def test_cleanup_is_bounded_and_never_replaces_the_original_error(
    monkeypatch, tmp_path, caplog
):
    """An edge that never answers: the first removal is stopped at the cleanup
    allowance, the rest are logged and left, and the timeout is what is raised."""
    clock = _Clock(monkeypatch)

    def wait_forever(order):
        clock.now += 45
        raise acme_errors.TimeoutError()

    _Ca(finalize=wait_forever).install(monkeypatch)

    class StalledPublisher(_Publisher):
        def absent(self, domain, token, *, timeout_seconds):
            clock.now += timeout_seconds
            super().absent(domain, token, timeout_seconds=timeout_seconds)

    publisher = StalledPublisher(fail_absent=True)
    lock = DeploymentLock(tmp_path / "deployment.lock")

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(
            ExecutionError, match="timed out during polling and finalization"
        ),
        lock,
    ):
        _issuer(tmp_path, publisher).issue(
            _site("a.example.com", "b.example.com", "c.example.com"),
            "ops@example.com",
        )

    assert clock.elapsed() <= 60
    assert [event[1] for event in publisher.events if event[0] == "absent"] == [
        "a.example.com"
    ]
    assert "b.example.com, c.example.com" in caplog.text
    # The deployment lock is free again; a held one raises DeploymentBusyError.
    with DeploymentLock(tmp_path / "deployment.lock"):
        pass


def test_the_account_directory_is_not_a_path_an_edge_writes():
    """A standalone host is controller and edge at once, and the edge's runtime
    paths sit beside the controller's state directory. Sharing one would hand
    the account key's directory to the edge role's owner and mode."""
    defaults = yaml.safe_load(
        (CORE_ANSIBLE / "roles/blitzecdn_edge/defaults/main.yml").read_text()
    )
    edge_paths = {
        Path(path) for path in defaults["blitzecdn_edge_runtime"]["paths"].values()
    }
    state = Path("/var/lib/blitzecdn")

    assert acme_account_dir(state) not in edge_paths
    assert certificate_dir(state) not in edge_paths
