from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.types import PublicKeyTypes

from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError, NotFoundError
from blitzecdn.core.filesystem import atomic_write_bytes
from blitzecdn.core.process import terminate_process_group
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn_certificates.certificates.domain import (
    CertificateInfo,
    CertificateSource,
)

_MAX_CERTIFICATE_BYTES = 1_048_576
_MAX_PRIVATE_KEY_BYTES = 262_144


class CertificateStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

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
        now = datetime.now(UTC)
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
            email=email,
        )
        # The chain and key are one logical value. Fixed filenames made two
        # otherwise-atomic replaces unsafe as a pair: a crash between them left
        # a new chain beside the old key. Immutable fingerprinted files plus a
        # metadata commit written last give readers either the complete old
        # pair or the complete new pair.
        atomic_write_bytes(directory / f"fullchain-{fingerprint}.pem", certificate_pem)
        atomic_write_bytes(directory / f"privkey-{fingerprint}.pem", private_key_pem)
        atomic_write_bytes(
            directory / "metadata.json",
            info.model_dump_json(indent=2).encode("utf-8") + b"\n",
        )
        self._discard_superseded_files(directory, fingerprint)
        return info

    def get(self, site_name: str) -> CertificateInfo:
        path = self._directory(site_name) / "metadata.json"
        if not path.is_file():
            raise NotFoundError(f"managed certificate for {site_name!r} does not exist")
        return CertificateInfo.model_validate_json(path.read_bytes())

    def list_all(self) -> list[CertificateInfo]:
        """Every certificate this store holds, oldest expiry first.

        Reads the store rather than the sites table on purpose: a certificate
        whose site was deleted still occupies disk and still expires, and an
        operator asking what they hold should be told about it. A directory
        without readable metadata is skipped — it is not a certificate we can
        say anything true about, and raising would make one damaged file hide
        every healthy one.
        """
        root = self._settings.certificate_dir
        if not root.is_dir():
            return []
        found: list[CertificateInfo] = []
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            try:
                found.append(self.get(directory.name))
            except (NotFoundError, OSError, ValueError):
                continue
        return sorted(found, key=lambda info: info.not_after)

    def sources(self, site_name: str) -> tuple[Path, Path]:
        directory = self._directory(site_name)
        info = self.get(site_name)
        certificate = directory / f"fullchain-{info.fingerprint_sha256}.pem"
        private_key = directory / f"privkey-{info.fingerprint_sha256}.pem"
        if not certificate.is_file() or not private_key.is_file():
            raise ConfigurationError(
                f"managed certificate files for {site_name!r} are missing"
            )
        return certificate, private_key

    @staticmethod
    def _discard_superseded_files(directory: Path, fingerprint: str) -> None:
        current = {
            f"fullchain-{fingerprint}.pem",
            f"privkey-{fingerprint}.pem",
        }
        for pattern in ("fullchain-*.pem", "privkey-*.pem"):
            for path in directory.glob(pattern):
                if path.name not in current:
                    # Cleanup is not part of the commit. Reporting a failed
                    # install after metadata switched to the new complete pair
                    # would invite an unnecessary and misleading retry.
                    with suppress(OSError):
                        path.unlink(missing_ok=True)

    def _directory(self, site_name: str) -> Path:
        return self._settings.certificate_dir / site_name

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

    @staticmethod
    def _validate_chain(certificates: list[x509.Certificate]) -> None:
        now = datetime.now(UTC)
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


class CertbotIssuer:
    def __init__(self, settings: Settings, certbot: str) -> None:
        self._settings = settings
        # Passed rather than read off `Settings`, which no longer carries it:
        # which certbot to run is this capability's configuration, and an
        # installation without this distribution has no certbot to name.
        self._certbot = certbot

    def issue(self, site: CdnSite, email: str) -> tuple[bytes, bytes]:
        if any(name.startswith("*.") for name in site.server_names):
            raise ConfigurationError(
                "HTTP-01 cannot issue wildcard certificates; upload one or use DNS-01"
            )
        executable = self._certbot
        if "/" in executable:
            available = Path(executable).is_file()
        else:
            available = shutil.which(executable) is not None
        if not available:
            raise ConfigurationError(
                f"certbot is not available: {executable}; install certbot on PATH"
            )
        config_dir = self._settings.state_dir / "letsencrypt/config"
        work_dir = self._settings.state_dir / "letsencrypt/work"
        logs_dir = self._settings.state_dir / "letsencrypt/logs"
        for directory in (config_dir, work_dir, logs_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        hook = shlex.join([sys.executable, "-m", "blitzecdn_certificates.acme_hook"])
        command = [
            executable,
            "certonly",
            "--manual",
            "--preferred-challenges",
            "http",
            "--manual-auth-hook",
            f"{hook} present",
            "--manual-cleanup-hook",
            f"{hook} absent",
            "--non-interactive",
            "--agree-tos",
            "--email",
            email,
            "--cert-name",
            site.name,
            "--config-dir",
            str(config_dir),
            "--work-dir",
            str(work_dir),
            "--logs-dir",
            str(logs_dir),
        ]
        for domain in site.server_names:
            command.extend(("--domain", domain))
        environment = os.environ.copy()
        environment["BLITZE_PROJECT_DIR"] = str(self._settings.project_dir)
        try:
            process = subprocess.Popen(  # noqa: S603 -- fixed argument array
                command,
                cwd=self._settings.project_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutionError(f"unable to execute certbot: {exc}") from exc
        try:
            stdout, stderr = process.communicate(
                timeout=self._settings.deployment_timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            terminate_process_group(process, process.communicate)
            raise ExecutionError("certificate request timed out") from exc
        if process.returncode != 0:
            detail = (stderr or stdout)[-4000:].strip()
            raise ExecutionError(f"certificate request failed: {detail}")
        live = config_dir / "live" / site.name
        try:
            return (
                (live / "fullchain.pem").read_bytes(),
                (live / "privkey.pem").read_bytes(),
            )
        except OSError as exc:
            raise ExecutionError(
                "certbot succeeded but certificate files are missing"
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
