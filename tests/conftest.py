from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from blitzecdn.config import Settings
from blitzecdn.infrastructure.ansible import CommandResult


class FakeRunner:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.results = results or [CommandResult(0, "ok", "")]
        self.check_modes: list[bool] = []

    def lock(self) -> nullcontext[None]:
        return nullcontext()

    def validate(self) -> CommandResult:
        return self.results[0]

    def run(self, *, check: bool) -> CommandResult:
        self.check_modes.append(check)
        return self.results.pop(0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    ansible = tmp_path / "ansible"
    (ansible / "inventory").mkdir(parents=True)
    (ansible / "playbooks").mkdir()
    (ansible / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    inventory = ansible / "inventory/hosts.yml"
    inventory.write_text(
        "all:\n  children:\n    blitzecdn_edges:\n      hosts: {}\n",
        encoding="utf-8",
    )
    playbook = ansible / "playbooks/edge.yml"
    playbook.write_text("- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8")
    state = tmp_path / "state"
    return Settings(
        project_dir=tmp_path,
        state_dir=state,
        database_path=state / "control-plane.db",
        ansible_dir=ansible,
        inventory_path=inventory,
        playbook_path=playbook,
        generated_vars_path=state / "desired-state.yml",
        deployment_lock_path=state / "deployment.lock",
        certificate_dir=state / "certificates",
        acme_challenge_playbook_path=ansible / "playbooks/acme-challenge.yml",
        ansible_playbook="/usr/bin/true",
        api_keys={"tester": "x" * 32},
    )


@pytest.fixture
def site_payload() -> dict[str, object]:
    return {
        "name": "example-cdn",
        "server_names": ["cdn.example.com"],
        "origin_host": "origin.example.com",
    }


@pytest.fixture
def certificate_pair():
    def generate(
        domains: tuple[str, ...] = ("cdn.example.com",),
        *,
        valid: bool = True,
    ) -> tuple[bytes, bytes]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        start = now - timedelta(days=1) if valid else now - timedelta(days=10)
        end = now + timedelta(days=30) if valid else now - timedelta(days=1)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])])
            )
            .issuer_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
            )
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name) for name in domains]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        return (
            certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )

    return generate
