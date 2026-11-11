"""ACME issuance in-process, through the ``acme`` library.

No subprocess and no hook: the HTTP-01 challenge is placed on the edges by a
direct call, so nothing inherits the controller's environment (issue 247) and
no value has to round-trip through a child's environment to reach a playbook.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

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


class ChallengePublisher(Protocol):
    """Where an HTTP-01 key authorization is served from while the CA checks it."""

    def present(self, domain: str, token: str, validation: str) -> None: ...

    def absent(self, domain: str, token: str) -> None: ...


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
        # Issuance runs under the deployment lock, within its budget.
        self._timeout_seconds = timeout_seconds
        self._ca_bundle = ca_bundle

    def issue(self, site: CdnSite, email: str) -> tuple[bytes, bytes]:
        if any(name.startswith("*.") for name in site.server_names):
            raise ConfigurationError(
                "HTTP-01 cannot issue wildcard certificates; upload one or use DNS-01"
            )
        # Naive local time: the library compares against `datetime.now()`.
        deadline = datetime.now() + timedelta(seconds=self._timeout_seconds)
        private_key = ec.generate_private_key(ec.SECP256R1())
        try:
            acme = self._client(email)
            order = acme.new_order(_csr(private_key, site.server_names))
            presented = self._present(acme, order)
            try:
                order = acme.poll_and_finalize(order, deadline=deadline)
            finally:
                for domain, token in presented:
                    self._challenges.absent(domain, token)
        except acme_errors.TimeoutError as exc:
            raise ExecutionError("certificate request timed out") from exc
        except acme_errors.ValidationError as exc:
            raise ExecutionError(
                "the CA could not validate: " + _validation_problems(exc)
            ) from exc
        except (acme_errors.Error, messages.Error, requests.RequestException) as exc:
            raise ExecutionError(f"certificate request failed: {exc}") from exc
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

    def _client(self, email: str) -> client.ClientV2:
        key = self._account_key()
        network = client.ClientNetwork(key, alg=ES256, user_agent=_USER_AGENT)
        if self._ca_bundle is not None:
            # A private ACME CA whose API certificate the system store lacks.
            network.session.verify = str(self._ca_bundle)
        directory = client.ClientV2.get_directory(self._directory_url, network)
        acme = client.ClientV2(directory, network)
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
        self, acme: client.ClientV2, order: messages.OrderResource
    ) -> list[tuple[str, str]]:
        """Place every pending HTTP-01 challenge, then tell the CA to check them.

        Returns what was placed, so the caller removes exactly that.
        """
        account_key = acme.net.key
        presented: list[tuple[str, str]] = []
        answers: list[tuple[messages.ChallengeBody, challenges.ChallengeResponse]] = []
        try:
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
                self._challenges.present(domain, token, validation)
                presented.append((domain, token))
                answers.append((challenge, response))
            for challenge, response in answers:
                acme.answer_challenge(challenge, response)
        except BaseException:
            for domain, token in presented:
                self._challenges.absent(domain, token)
            raise
        return presented

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
