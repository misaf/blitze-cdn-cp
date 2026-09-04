from __future__ import annotations

import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from blitzecdn_certificates.certificates.adapters import CertbotIssuer, CertificateStore
from blitzecdn_certificates.certificates.adapters import store as certificates_module
from blitzecdn_certificates.certificates.domain import CertificateSource
from control_plane_fixtures import private_key_pem
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError, NotFoundError


def test_store_validates_and_persists_managed_certificate(
    settings, site_payload, certificate_pair
):
    site = CdnSite.model_validate(site_payload)
    certificate, key = certificate_pair()
    store = CertificateStore(settings)

    info = store.install(site, certificate, key, source=CertificateSource.UPLOADED)

    assert info.site == site.name
    assert info.domains == ("cdn.example.com",)
    assert store.get(site.name) == info
    certificate_path, key_path = store.sources(site.name)
    assert certificate_path.read_bytes() == certificate
    assert key_path.read_bytes() == key
    assert os.stat(key_path).st_mode & 0o777 == 0o600


def test_store_commits_a_certificate_pair_only_after_both_files_exist(
    settings, site_payload, certificate_pair, monkeypatch
):
    site = CdnSite.model_validate(site_payload)
    store = CertificateStore(settings)
    old_certificate, old_key = certificate_pair(days=40)
    old_info = store.install(
        site, old_certificate, old_key, source=CertificateSource.UPLOADED
    )
    old_sources = store.sources(site.name)
    replacement_certificate, replacement_key = certificate_pair(days=80)
    real_write = certificates_module.atomic_write_bytes

    def interrupted_write(path, payload, *, mode=0o600):
        if path.name.startswith("privkey-"):
            raise OSError("simulated process interruption")
        real_write(path, payload, mode=mode)

    monkeypatch.setattr(certificates_module, "atomic_write_bytes", interrupted_write)
    with pytest.raises(OSError, match="process interruption"):
        store.install(
            site,
            replacement_certificate,
            replacement_key,
            source=CertificateSource.UPLOADED,
        )

    assert store.get(site.name) == old_info
    assert store.sources(site.name) == old_sources
    assert old_sources[0].read_bytes() == old_certificate
    assert old_sources[1].read_bytes() == old_key


def test_store_rejects_invalid_mismatched_expired_and_wrong_domain(
    settings, site_payload, certificate_pair, rsa_keys
):
    site = CdnSite.model_validate(site_payload)
    store = CertificateStore(settings)
    certificate, key = certificate_pair()
    # A key from the pool that this certificate was demonstrably not signed
    # with — `certificate_pair` hands out `rsa_keys[0]` first.
    wrong_key_pem = private_key_pem(rsa_keys[-1])
    assert wrong_key_pem != key
    with pytest.raises(ConfigurationError, match="do not match"):
        store.install(
            site,
            certificate,
            wrong_key_pem,
            source=CertificateSource.UPLOADED,
        )
    wrong_certificate, wrong_domain_key = certificate_pair(("other.example.com",))
    with pytest.raises(ConfigurationError, match="does not cover"):
        store.install(
            site,
            wrong_certificate,
            wrong_domain_key,
            source=CertificateSource.UPLOADED,
        )
    expired, expired_key = certificate_pair(valid=False)
    with pytest.raises(ConfigurationError, match="not currently valid"):
        store.install(site, expired, expired_key, source=CertificateSource.UPLOADED)
    with pytest.raises(ConfigurationError, match="invalid unencrypted PEM"):
        store.install(site, b"bad", b"bad", source=CertificateSource.UPLOADED)
    with pytest.raises(NotFoundError):
        store.get("missing")


def test_store_validates_certificate_chain(settings, site_payload, rsa_keys):
    now = datetime.now(UTC)
    issuer_key = rsa_keys[0]
    issuer_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    issuer = (
        x509.CertificateBuilder()
        .subject_name(issuer_name)
        .issuer_name(issuer_name)
        .public_key(issuer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )
    leaf_key = rsa_keys[1]
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cdn.example.com")])
        )
        .issuer_name(issuer_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("cdn.example.com")]),
            critical=False,
        )
        .sign(issuer_key, hashes.SHA256())
    )
    key = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    chain = leaf.public_bytes(serialization.Encoding.PEM) + issuer.public_bytes(
        serialization.Encoding.PEM
    )
    site = CdnSite.model_validate(site_payload)

    CertificateStore(settings).install(
        site, chain, key, source=CertificateSource.UPLOADED
    )

    unrelated_key = rsa_keys[2]
    unrelated = (
        x509.CertificateBuilder()
        .subject_name(issuer_name)
        .issuer_name(issuer_name)
        .public_key(unrelated_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(unrelated_key, hashes.SHA256())
    )
    invalid_chain = leaf.public_bytes(
        serialization.Encoding.PEM
    ) + unrelated.public_bytes(serialization.Encoding.PEM)
    with pytest.raises(ConfigurationError, match="signatures do not match"):
        CertificateStore(settings).install(
            site, invalid_chain, key, source=CertificateSource.UPLOADED
        )


def test_certbot_issuer_builds_http01_command(
    settings, site_payload, certificate_pair, monkeypatch
):
    executable = settings.project_dir / "certbot"
    executable.touch()
    configured = settings.model_copy(update={"certbot": str(executable)})
    certificate, key = certificate_pair()
    captured: list[str] = []

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, timeout=None):
            return "ok", ""

    def fake_popen(command, **_kwargs):
        captured.extend(command)
        config_dir = command[command.index("--config-dir") + 1]
        live = settings.project_dir.__class__(config_dir) / "live/cdn-example-com"
        live.mkdir(parents=True)
        (live / "fullchain.pem").write_bytes(certificate)
        (live / "privkey.pem").write_bytes(key)
        return FakeProcess()

    monkeypatch.setattr(certificates_module.subprocess, "Popen", fake_popen)
    result = CertbotIssuer(configured, "certbot").issue(
        CdnSite.model_validate(site_payload), "owner@example.com"
    )
    assert result == (certificate, key)
    assert "--manual-auth-hook" in captured
    assert captured[captured.index("--domain") + 1] == "cdn.example.com"


def test_certbot_issuer_rejects_wildcards_and_reports_failure(
    settings, site_payload, monkeypatch
):
    executable = settings.project_dir / "certbot"
    executable.touch()
    configured = settings.model_copy(update={"certbot": str(executable)})
    wildcard = CdnSite.model_validate(
        {**site_payload, "server_names": ["*.example.com"]}
    )
    with pytest.raises(ConfigurationError, match="wildcard"):
        CertbotIssuer(configured, "certbot").issue(wildcard, "owner@example.com")

    class FailedProcess:
        pid = 123
        returncode = 1

        def communicate(self, timeout=None):
            return "", "failed"

    monkeypatch.setattr(
        certificates_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FailedProcess(),
    )
    with pytest.raises(ExecutionError, match="failed"):
        CertbotIssuer(configured, "certbot").issue(
            CdnSite.model_validate(site_payload), "owner@example.com"
        )


def test_certbot_timeout_terminates_process_group(settings, site_payload, monkeypatch):
    executable = settings.project_dir / "certbot"
    executable.touch()
    configured = settings.model_copy(update={"certbot": str(executable)})
    signals: list[int] = []

    class TimedOutProcess:
        pid = 456
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("certbot", timeout)
            return "", ""

    monkeypatch.setattr(
        certificates_module.subprocess,
        "Popen",
        lambda *args, **kwargs: TimedOutProcess(),
    )
    monkeypatch.setattr(
        certificates_module.os,
        "killpg",
        lambda _pid, sent_signal: signals.append(sent_signal),
    )

    with pytest.raises(ExecutionError, match="timed out"):
        CertbotIssuer(configured, "certbot").issue(
            CdnSite.model_validate(site_payload), "owner@example.com"
        )

    assert signals == [signal.SIGTERM]


def _install(store, settings, certificate_pair, name, *, days, source):
    site = CdnSite.model_validate(
        {
            "name": name,
            "server_names": [f"{name}.example.com"],
            "origin_host": "origin.example.com",
        }
    )
    certificate, key = certificate_pair((f"{name}.example.com",), days=days)
    return store.install(site, certificate, key, source=source, email="ops@example.com")


def test_the_store_lists_everything_it_holds_soonest_expiry_first(
    settings, certificate_pair
):
    store = CertificateStore(settings)
    _install(
        store,
        settings,
        certificate_pair,
        "late",
        days=80,
        source=CertificateSource.ACME,
    )
    _install(
        store,
        settings,
        certificate_pair,
        "soon",
        days=5,
        source=CertificateSource.UPLOADED,
    )

    assert [info.site for info in store.list_all()] == ["soon", "late"]


def test_a_damaged_certificate_directory_does_not_hide_the_healthy_ones(
    settings, certificate_pair
):
    store = CertificateStore(settings)
    _install(
        store,
        settings,
        certificate_pair,
        "good",
        days=40,
        source=CertificateSource.ACME,
    )
    broken = settings.certificate_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "metadata.json").write_text("{not json", encoding="utf-8")
    (settings.certificate_dir / "empty").mkdir()

    assert [info.site for info in store.list_all()] == ["good"]
