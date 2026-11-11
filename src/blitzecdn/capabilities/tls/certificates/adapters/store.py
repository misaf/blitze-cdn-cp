from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.types import PublicKeyTypes

from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateInfo,
    CertificateSource,
)
from blitzecdn.capabilities.tls.contract import (
    MANAGED_TLS_ROOT,
    CertificateVersion,
    managed_certificate_paths,
)
from blitzecdn.capabilities.tls.settings import certificate_dir
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import ConfigurationError, NotFoundError
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.runtime.filesystem import atomic_write_bytes

_MAX_CERTIFICATE_BYTES = 1_048_576
_MAX_PRIVATE_KEY_BYTES = 262_144


class CertificateStore:
    def __init__(self, settings: Settings, *, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock

    def install(
        self,
        site: CdnSite,
        certificate_pem: bytes,
        private_key_pem: bytes,
        *,
        source: CertificateSource,
        email: str | None = None,
    ) -> CertificateInfo:
        if len(certificate_pem) > _MAX_CERTIFICATE_BYTES:
            raise ConfigurationError("certificate chain exceeds 1 MiB")
        if len(private_key_pem) > _MAX_PRIVATE_KEY_BYTES:
            raise ConfigurationError("private key exceeds 256 KiB")
        try:
            certificates = x509.load_pem_x509_certificates(certificate_pem)
            private_key = serialization.load_pem_private_key(
                private_key_pem, password=None
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "invalid unencrypted PEM certificate or key"
            ) from exc
        if not certificates:
            raise ConfigurationError("certificate chain is empty")
        leaf = certificates[0]
        self._validate_key_pair(leaf.public_key(), private_key.public_key())
        self._validate_chain(certificates)
        domains = self._certificate_domains(leaf)
        missing = [name for name in site.server_names if not _covered(name, domains)]
        if missing:
            raise ConfigurationError(
                "certificate does not cover: " + ", ".join(sorted(missing))
            )
        now = self._clock()
        if leaf.not_valid_before_utc > now or leaf.not_valid_after_utc <= now:
            raise ConfigurationError("certificate is not currently valid")
        directory = self._directory(site.name)
        fingerprint = leaf.fingerprint(hashes.SHA256()).hex()
        info = CertificateInfo(
            site=site.name,
            source=source,
            domains=tuple(sorted(domains)),
            not_before=leaf.not_valid_before_utc,
            not_after=leaf.not_valid_after_utc,
            fingerprint_sha256=fingerprint,
            certificate_digest=hashlib.sha256(certificate_pem).hexdigest(),
            private_key_digest=hashlib.sha256(private_key_pem).hexdigest(),
            email=email,
        )
        # Chain and key are one value: immutable fingerprinted files plus metadata
        # written last give readers the complete old pair or the complete new pair.
        self._write_version(
            directory / f"fullchain-{info.certificate_digest}.pem", certificate_pem
        )
        self._write_version(
            directory / f"privkey-{info.private_key_digest}.pem", private_key_pem
        )
        atomic_write_bytes(
            directory / "metadata.json",
            info.model_dump_json(indent=2).encode("utf-8") + b"\n",
        )
        return info

    def get(self, site_name: str) -> CertificateInfo:
        path = self._directory(site_name) / "metadata.json"
        if not path.is_file():
            raise NotFoundError(f"managed certificate for {site_name!r} does not exist")
        return CertificateInfo.model_validate_json(path.read_bytes())

    def list_all(self) -> list[CertificateInfo]:
        """Every certificate held on disk, oldest expiry first, including orphaned ones.

        Unreadable metadata is skipped so one damaged file hides nothing else.
        """
        root = certificate_dir(self._settings.state_dir)
        if not root.is_dir():
            return []
        found: list[CertificateInfo] = []
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            try:
                found.append(self.get(directory.name))
            except NotFoundError, OSError, ValueError:
                continue
        return sorted(found, key=lambda info: info.not_after)

    def sources(
        self, site_name: str, *, version: CertificateVersion | None = None
    ) -> tuple[Path, Path]:
        directory = self._directory(site_name)
        info = version if version is not None else self.get(site_name).version
        certificate = directory / f"fullchain-{info.certificate_digest}.pem"
        private_key = directory / f"privkey-{info.private_key_digest}.pem"
        if not certificate.is_file() or not private_key.is_file():
            raise ConfigurationError(
                f"managed certificate files for {site_name!r} are missing"
            )
        for path, expected in (
            (certificate, info.certificate_digest),
            (private_key, info.private_key_digest),
        ):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ConfigurationError(
                    f"certificate material failed integrity: {path.name}"
                )
        return certificate, private_key

    def collect(self, referenced_paths: Sequence[str]) -> tuple[str, ...]:
        """Delete only recognizable immutable files absent from every root.

        The caller holds the deployment lock and write transaction. Every metadata root
        is validated before anything is deleted; symlinks and unknown files are never
        traversed.
        """
        root = certificate_dir(self._settings.state_dir)
        if not root.is_dir() or root.is_symlink():
            return ()
        retained = set(referenced_paths)
        candidates: list[Path] = []
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            metadata = directory / "metadata.json"
            if metadata.is_symlink():
                raise ConfigurationError("certificate metadata must not be a symlink")
            if metadata.exists():
                info = self.get(directory.name)
                if info.site != directory.name:
                    raise ConfigurationError(
                        "certificate metadata site does not match its directory"
                    )
                retained.update(managed_certificate_paths(info.site, info.version))
            for path in sorted(directory.iterdir()):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and re.fullmatch(
                        r"(?:fullchain|privkey)-[0-9a-f]{64}\.pem", path.name
                    )
                ):
                    edge_path = f"{MANAGED_TLS_ROOT}/{directory.name}/{path.name}"
                    if edge_path not in retained:
                        candidates.append(path)
        removed = []
        for path in candidates:
            path.unlink()
            removed.append(path.relative_to(root).as_posix())
        return tuple(removed)

    @staticmethod
    def _write_version(path: Path, payload: bytes) -> None:
        """Write a fingerprinted version; never overwrite one a release may name."""
        if path.exists():
            if path.read_bytes() != payload:
                raise ConfigurationError(
                    f"certificate material failed integrity: {path.name}"
                )
            return
        atomic_write_bytes(path, payload)

    def _directory(self, site_name: str) -> Path:
        return certificate_dir(self._settings.state_dir) / site_name

    @staticmethod
    def _validate_key_pair(
        certificate_key: PublicKeyTypes, private_key: PublicKeyTypes
    ) -> None:
        certificate_public = certificate_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_public = private_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if certificate_public != private_public:
            raise ConfigurationError("certificate and private key do not match")

    @staticmethod
    def _certificate_domains(certificate: x509.Certificate) -> set[str]:
        try:
            san = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
        except x509.ExtensionNotFound as exc:
            raise ConfigurationError(
                "certificate has no subjectAltName extension"
            ) from exc
        names = {
            name.lower().rstrip(".") for name in san.get_values_for_type(x509.DNSName)
        }
        names.update(str(value) for value in san.get_values_for_type(x509.IPAddress))
        return names

    def _validate_chain(self, certificates: list[x509.Certificate]) -> None:
        now = self._clock()
        for child, issuer in pairwise(certificates):
            if issuer.not_valid_before_utc > now or issuer.not_valid_after_utc <= now:
                raise ConfigurationError("certificate chain contains an invalid issuer")
            try:
                constraints = issuer.extensions.get_extension_for_class(
                    x509.BasicConstraints
                ).value
            except x509.ExtensionNotFound as exc:
                raise ConfigurationError(
                    "certificate chain issuer has no basicConstraints extension"
                ) from exc
            if not constraints.ca:
                raise ConfigurationError("certificate chain issuer is not a CA")
            try:
                child.verify_directly_issued_by(issuer)
            except (InvalidSignature, TypeError, ValueError) as exc:
                raise ConfigurationError(
                    "certificate chain is not ordered or signatures do not match"
                ) from exc


def _covered(requested: str, certificate_names: set[str]) -> bool:
    requested = requested.lower().rstrip(".")
    if requested in certificate_names:
        return True
    if requested.startswith("*."):
        return False
    try:
        ipaddress.ip_address(requested)
        return False
    except ValueError:
        pass
    labels = requested.split(".", 1)
    return len(labels) == 2 and f"*.{labels[1]}" in certificate_names
