from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from support.control_plane import certificate_dir, private_key_pem
from support.policy import site_from_settings

from blitzecdn.capabilities.tls.certificates.adapters import (
    CertificateStore,
)
from blitzecdn.capabilities.tls.certificates.adapters import (
    store as certificates_module,
)
from blitzecdn.capabilities.tls.certificates.domain import CertificateSource
from blitzecdn.core.exceptions import ConfigurationError, NotFoundError
from blitzecdn.core.ports.clock import system_clock


def test_store_validates_and_persists_managed_certificate(
    settings, site_payload, certificate_pair
):
    site = site_from_settings(site_payload)
    certificate, key = certificate_pair()
    store = CertificateStore(settings, clock=system_clock)

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
    site = site_from_settings(site_payload)
    store = CertificateStore(settings, clock=system_clock)
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


def test_replacement_retains_every_file_an_older_release_can_reference(
    settings, site_payload, certificate_pair
):
    site = site_from_settings(site_payload)
    store = CertificateStore(settings, clock=system_clock)
    old_certificate, old_key = certificate_pair(days=40)
    store.install(site, old_certificate, old_key, source=CertificateSource.UPLOADED)
    old_sources = store.sources(site.name)
    certificate, key = certificate_pair(days=80)

    store.install(site, certificate, key, source=CertificateSource.UPLOADED)

    assert store.sources(site.name) != old_sources
    assert old_sources[0].read_bytes() == old_certificate
    assert old_sources[1].read_bytes() == old_key


def test_material_is_content_addressed_and_corruption_is_refused(
    settings, site_payload, certificate_pair
):
    site = site_from_settings(site_payload)
    store = CertificateStore(settings, clock=system_clock)
    certificate, key = certificate_pair()
    info = store.install(site, certificate, key, source=CertificateSource.UPLOADED)
    chain_path, key_path = store.sources(site.name)
    assert chain_path.name == f"fullchain-{info.certificate_digest}.pem"
    assert key_path.name == f"privkey-{info.private_key_digest}.pem"
    chain_path.write_bytes(b"damaged")

    with pytest.raises(ConfigurationError, match="integrity"):
        store.sources(site.name)
    with pytest.raises(ConfigurationError, match="integrity"):
        store.install(site, certificate, key, source=CertificateSource.UPLOADED)


def test_store_rejects_invalid_mismatched_expired_and_wrong_domain(
    settings, site_payload, certificate_pair, rsa_keys
):
    site = site_from_settings(site_payload)
    store = CertificateStore(settings, clock=system_clock)
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
    site = site_from_settings(site_payload)

    CertificateStore(settings, clock=system_clock).install(
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
        CertificateStore(settings, clock=system_clock).install(
            site, invalid_chain, key, source=CertificateSource.UPLOADED
        )


def _install(store, settings, certificate_pair, name, *, days, source):
    site = site_from_settings(
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
    store = CertificateStore(settings, clock=system_clock)
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
    store = CertificateStore(settings, clock=system_clock)
    _install(
        store,
        settings,
        certificate_pair,
        "good",
        days=40,
        source=CertificateSource.ACME,
    )
    broken = certificate_dir(settings) / "broken"
    broken.mkdir(parents=True)
    (broken / "metadata.json").write_text("{not json", encoding="utf-8")
    (certificate_dir(settings) / "empty").mkdir()

    assert [info.site for info in store.list_all()] == ["good"]
