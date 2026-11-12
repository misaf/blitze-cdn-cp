"""ACME issuance in-process, through the ``acme`` library.

No subprocess and no hook: the HTTP-01 challenge is placed on the edges by a
direct call, so nothing inherits the controller's environment (issue 247) and
no value has to round-trip through a child's environment to reach a playbook.

Issuance holds the deployment lock, so every step is bounded by what is left of
one budget, measured on the monotonic clock. Left alone, the steps are not:

=========================  ==================================================
Step                       Bound without this module's budget
=========================  ==================================================
Directory discovery        one GET, ``requests`` timeout of 45 s (the library's
                           ``DEFAULT_NETWORK_TIMEOUT``) per connect and per read
Account registration       a nonce HEAD and a POST, 45 s each; a ``badNonce``
                           retry adds another POST
New order                  a POST and one POST-as-GET per authorization, 45 s each
Placing a challenge        one ``acme-challenge`` play per name, stopped by
                           Ansible Runner at the deployment timeout (900 s by
                           default): one play could spend the whole budget
Answering a challenge      one POST per name, 45 s each
Polling and finalization   the library's wall-clock ``deadline``, checked between
                           requests of 45 s each, with 1 s sleeps between polls
Removing a challenge       one play per name at the deployment timeout, with no
                           overall limit
=========================  ==================================================

What this module does instead:

* Every HTTP request the library sends gets a timeout of the budget left,
  capped at the library's 45 s, and none is sent once the budget is spent
  (``_BudgetedSession``). It is a connect and per-read timeout, as ``requests``
  offers no other: a stall is cut off, a CA that trickles bytes is not.
* Every challenge play is stopped by Ansible Runner at the budget left.
* Polling gets a wall-clock deadline computed from the monotonic budget at the
  moment it starts, as the library compares against ``datetime.now()``; the
  session above still refuses requests if the wall clock moves.
* Cleanup has an allowance of its own, reserved out of the total so issuance
  plus cleanup never exceed the timeout the lock's lease assumes: a quarter of
  the total, at most two minutes. Removals run one after another, each stopped
  at what is left of it; a token the allowance leaves no room for stays on its
  edge, logged, where it answers nothing the CA will ask again.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import requests
from acme import challenges, client, messages
from acme import errors as acme_errors
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from josepy.jwa import ES256
from josepy.jwk import JWKEC

from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError
from blitzecdn.core.runtime.filesystem import atomic_write_bytes

__all__ = ["AcmeIssuer", "ChallengePublisher"]

_USER_AGENT = "blitzecdn"
_LOGGER = logging.getLogger(__name__)

#: The longest one HTTP request to the CA may wait: the library's own default.
_REQUEST_SECONDS = float(client.DEFAULT_NETWORK_TIMEOUT)
#: Cleanup's allowance is reserved out of the total: this share, at most the cap.
_CLEANUP_SHARE = 4
_CLEANUP_CAP_SECONDS = 120

# Read through the module so a test can drive the budget without sleeping.
_monotonic = time.monotonic


class ChallengePublisher(Protocol):
    """Where an HTTP-01 key authorization is served from while the CA checks it.

    Each call returns or raises within ``timeout_seconds``.
    """

    def present(
        self, domain: str, token: str, validation: str, *, timeout_seconds: int
    ) -> None: ...

    def absent(self, domain: str, token: str, *, timeout_seconds: int) -> None: ...


class _Budget:
    """Seconds left of a fixed allowance, on the monotonic clock."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._ends = _monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._ends - _monotonic())

    def check(self) -> None:
        """Refuse to start another step once the budget is spent."""
        if self.remaining() <= 0:
            raise acme_errors.TimeoutError()

    def whole_seconds(self) -> int:
        """What is left, for an API that counts whole seconds; never rounds up."""
        left = math.floor(self.remaining())
        if left < 1:
            raise acme_errors.TimeoutError()
        return left

    def deadline(self) -> datetime:
        """The budget as the library's naive wall-clock deadline, from now."""
        return datetime.now() + timedelta(seconds=self.remaining())


class _BudgetedSession(requests.Session):
    """Gives each request the budget left, capped at the library's default.

    The library sets ``timeout`` on every request it sends, so this replaces it
    here rather than on the session, where it would be overridden.
    """

    def __init__(self, budget: _Budget) -> None:
        super().__init__()
        self._budget = budget

    def request(  # type: ignore[override]
        self, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        self._budget.check()
        kwargs["timeout"] = min(self._budget.remaining(), _REQUEST_SECONDS)
        return super().request(method, url, *args, **kwargs)


class _Steps:
    """Which step issuance is in, so a timeout can say where it happened."""

    def __init__(self, budget: _Budget) -> None:
        self._budget = budget
        self.current = "starting"

    def begin(self, name: str) -> None:
        self.current = name
        self._budget.check()


class AcmeIssuer:
    """Orders a certificate for every name a site serves, validated over HTTP-01.

    The ACME account key is created on first use and kept in ``account_dir``,
    which backup carries. Each issuance generates a fresh P-256 key for the
    certificate; nothing about an earlier order is reused.
    """

    def __init__(
        self,
        *,
        directory_url: str,
        account_dir: Path,
        challenges: ChallengePublisher,
        timeout_seconds: int,
        ca_bundle: Path | None = None,
    ) -> None:
        self._directory_url = directory_url
        self._account_dir = account_dir
        self._challenges = challenges
        # Issuance runs under the deployment lock, within its budget; cleanup's
        # allowance comes out of it, so the two together stay inside it.
        self._cleanup_seconds = min(
            _CLEANUP_CAP_SECONDS, timeout_seconds // _CLEANUP_SHARE
        )
        self._work_seconds = timeout_seconds - self._cleanup_seconds
        self._ca_bundle = ca_bundle

    def issue(self, site: CdnSite, email: str) -> tuple[bytes, bytes]:
        if any(name.startswith("*.") for name in site.server_names):
            raise ConfigurationError(
                "HTTP-01 cannot issue wildcard certificates; upload one or use DNS-01"
            )
        budget = _Budget(self._work_seconds)
        steps = _Steps(budget)
        private_key = ec.generate_private_key(ec.SECP256R1())
        presented: list[tuple[str, str]] = []
        try:
            acme = self._client(email, budget, steps)
            steps.begin("new order")
            order = acme.new_order(_csr(private_key, site.server_names))
            self._present(acme, order, presented, budget, steps)
            steps.begin("polling and finalization")
            order = acme.poll_and_finalize(order, deadline=budget.deadline())
        except (acme_errors.TimeoutError, requests.Timeout) as exc:
            raise ExecutionError(
                f"certificate request timed out during {steps.current} "
                f"(issuance budget {budget.seconds:.0f}s)"
            ) from exc
        except acme_errors.ValidationError as exc:
            raise ExecutionError(
                "the CA could not validate: " + _validation_problems(exc)
            ) from exc
        except (acme_errors.Error, messages.Error, requests.RequestException) as exc:
            raise ExecutionError(f"certificate request failed: {exc}") from exc
        finally:
            # Whatever happened, and never in place of it: a certificate the CA
            # already issued is kept, and the original error is what is raised.
            # Bounded by its own allowance, which the work budget left room for.
            self._remove(presented, _Budget(self._cleanup_seconds))
        if not order.fullchain_pem:
            raise ExecutionError("the CA finalized the order without a certificate")
        return (
            order.fullchain_pem.encode("ascii"),
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )

    def _client(self, email: str, budget: _Budget, steps: _Steps) -> client.ClientV2:
        key = self._account_key()
        network = client.ClientNetwork(key, alg=ES256, user_agent=_USER_AGENT)
        unbounded, network.session = network.session, _BudgetedSession(budget)
        unbounded.close()
        if self._ca_bundle is not None:
            # A private ACME CA whose API certificate the system store lacks.
            # `verify_ssl` is what the library passes to requests as `verify`,
            # overriding the session's, and requests takes a bundle path there.
            network.verify_ssl = str(self._ca_bundle)  # type: ignore[assignment]
        steps.begin("directory discovery")
        directory = client.ClientV2.get_directory(self._directory_url, network)
        acme = client.ClientV2(directory, network)
        steps.begin("account registration")
        registration = messages.NewRegistration.from_data(
            email=email, terms_of_service_agreed=True
        )
        try:
            network.account = acme.new_account(registration)
        except acme_errors.ConflictError as exists:
            # The key is already registered; the CA names the account's URL.
            network.account = messages.RegistrationResource(
                uri=exists.location, body=messages.Registration()
            )
        return acme

    def _present(
        self,
        acme: client.ClientV2,
        order: messages.OrderResource,
        presented: list[tuple[str, str]],
        budget: _Budget,
        steps: _Steps,
    ) -> None:
        """Place every pending HTTP-01 challenge, then tell the CA to check them.

        Each placement is appended to ``presented`` as it starts, so the caller
        removes whatever may have reached the edges, however far this got.
        """
        account_key = acme.net.key
        answers: list[
            tuple[str, messages.ChallengeBody, challenges.ChallengeResponse]
        ] = []
        for authorization in order.authorizations:
            if authorization.body.status == messages.STATUS_VALID:
                continue
            domain = authorization.body.identifier.value
            challenge = next(
                (
                    body
                    for body in authorization.body.challenges
                    if isinstance(body.chall, challenges.HTTP01)
                ),
                None,
            )
            if challenge is None:
                raise ExecutionError(
                    f"the CA offered no HTTP-01 challenge for {domain}"
                )
            response, validation = challenge.response_and_validation(account_key)
            token = challenge.chall.encode("token")
            steps.begin(f"placing the challenge for {domain}")
            # Recorded before the play runs: one stopped part-way may still have
            # written the token to some edges.
            presented.append((domain, token))
            self._challenges.present(
                domain, token, validation, timeout_seconds=budget.whole_seconds()
            )
            answers.append((domain, challenge, response))
        for domain, challenge, response in answers:
            steps.begin(f"answering the challenge for {domain}")
            acme.answer_challenge(challenge, response)

    def _remove(self, presented: list[tuple[str, str]], allowance: _Budget) -> None:
        """Take each placed challenge off, independently; a failure is logged.

        A stale token on an edge answers nothing the CA will ask again, so it is
        not worth an issued certificate or the error that ended the attempt, nor
        holding the deployment lock past ``allowance``.
        """
        for index, (domain, token) in enumerate(presented):
            left = math.floor(allowance.remaining())
            if left < 1:
                _LOGGER.warning(
                    "cleanup allowance of %ss spent; HTTP-01 challenges for %s "
                    "stay on the edge but answer nothing the CA will ask again",
                    f"{allowance.seconds:.0f}",
                    ", ".join(name for name, _token in presented[index:]),
                )
                return
            try:
                self._challenges.absent(domain, token, timeout_seconds=left)
            except Exception:
                _LOGGER.warning(
                    "could not remove the HTTP-01 challenge for %s; the token "
                    "stays on the edge but answers nothing the CA will ask again",
                    domain,
                    exc_info=True,
                )

    def _account_key(self) -> JWKEC:
        path = self._account_dir / "account-key.pem"
        if path.is_file():
            loaded = serialization.load_pem_private_key(
                path.read_bytes(), password=None
            )
            if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                raise ConfigurationError(f"{path} is not an EC private key")
            return JWKEC(key=loaded)
        self._account_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        generated = ec.generate_private_key(ec.SECP256R1())
        atomic_write_bytes(
            path,
            generated.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        return JWKEC(key=generated)


def _csr(key: ec.EllipticCurvePrivateKey, names: tuple[str, ...]) -> bytes:
    """A CSR naming every host in its SAN; the subject is empty, as CAs ignore it."""
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .add_extension(x509.SubjectAlternativeName(entries), critical=False)
        .sign(key, hashes.SHA256())
    )
    return request.public_bytes(serialization.Encoding.PEM)


def _validation_problems(error: acme_errors.ValidationError) -> str:
    """Each failed name with the CA's own problem detail, which says why."""
    problems = []
    for authorization in error.failed_authzrs:
        domain = authorization.body.identifier.value
        details = [
            str(body.error)
            for body in authorization.body.challenges
            if body.error is not None
        ]
        problems.append(f"{domain}: {'; '.join(details) or 'no detail given'}")
    return ", ".join(problems) or "no authorization detail given"
