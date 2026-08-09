from __future__ import annotations

import json
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
        self.host_limits: list[str | None] = []
        self.purges: list[tuple[list[dict[str, str]], bool, str | None]] = []
        self.stats_runs: list[tuple[Path, str | None]] = []
        self.decommissions: list[str] = []
        self.edge_reports: dict[str, object] = {}

    def lock(self) -> nullcontext[None]:
        return nullcontext()

    def validate(self) -> CommandResult:
        return self.results[0]

    def run(self, *, check: bool, host_limit: str | None = None) -> CommandResult:
        self.check_modes.append(check)
        self.host_limits.append(host_limit)
        return self.results.pop(0)

    def run_cache_purge(
        self,
        *,
        entries: list[dict[str, str]],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> CommandResult:
        self.purges.append((entries, purge_all, host_limit))
        return self.results.pop(0)

    def run_decommission(self, *, host_limit: str) -> CommandResult:
        self.decommissions.append(host_limit)
        return self.results.pop(0)

    def run_stats(
        self, *, output_dir: Path, host_limit: str | None = None
    ) -> CommandResult:
        self.stats_runs.append((output_dir, host_limit))
        # Stand in for the edges writing their reports: the real roles deliver
        # these files, so a double that only returns a recap would exercise a
        # code path that never happens.
        for host, document in self.edge_reports.items():
            (output_dir / f"{host}.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
        return self.results.pop(0)


class FakePreflight:
    """Stands in for ``CertificatePreflight`` without touching the network.

    Certificate preflight resolves hostnames, queries CAA and probes origins,
    none of which a test can do. The default is a clean report so tests about
    issuance stay about issuance; ``failures`` makes it block, for the tests
    that are about the refusal itself.
    """

    def __init__(self, failures: tuple[str, ...] = ()) -> None:
        self.failures = failures
        self.calls: list[tuple[str, bool, int | None]] = []

    def check(self, site, *, deployed: bool, record_ttl: int | None = None):
        from blitzecdn.domain.models import (
            PreflightCheck,
            PreflightReport,
            PreflightSeverity,
        )

        self.calls.append((site.name, deployed, record_ttl))
        return PreflightReport(
            site=site.name,
            checks=tuple(
                PreflightCheck(
                    name=name,
                    passed=False,
                    severity=PreflightSeverity.BLOCKING,
                    detail=f"{name} failed",
                )
                for name in self.failures
            ),
        )


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
        cache_purge_playbook_path=ansible / "playbooks/cache-purge.yml",
        stats_playbook_path=ansible / "playbooks/stats.yml",
        decommission_playbook_path=ansible / "playbooks/decommission.yml",
        ansible_playbook="/usr/bin/true",
        api_keys={"tester": "x" * 32},
    )


@pytest.fixture
def site_payload() -> dict[str, object]:
    """A site as a proxied record derives it.

    ``name`` is not free-form any more: it is what ``derive_site_name`` produces
    for ``cdn.example.com``, so a test that builds a site by hand still matches
    one the control plane would derive.
    """
    return {
        "name": "cdn-example-com",
        "server_names": ["cdn.example.com"],
        "origin_host": "198.51.100.10",
    }


@pytest.fixture
def domain_payload() -> dict[str, object]:
    return {"name": "example.com"}


@pytest.fixture
def record_payload() -> dict[str, object]:
    return {
        "domain": "example.com",
        "name": "cdn",
        "type": "A",
        "value": "198.51.100.10",
        "proxied": True,
    }


@pytest.fixture
def seeded(settings):
    """A control plane holding one zone with one proxied record.

    Returns ``(control, repository)``. Most tests need a site to exist, and the
    only supported way to make one now is to proxy a record.
    """

    def build(runner=None):
        from blitzecdn.application import ControlPlane
        from blitzecdn.domain.models import DnsRecord, Domain
        from blitzecdn.infrastructure.database import Repository

        repository = Repository(settings.database_path)
        control = ControlPlane(
            settings,
            repository,
            runner or FakeRunner(),
            preflight=FakePreflight(),
        )
        control.create_domain(Domain(name="example.com"), "tester")
        control.create_record(
            DnsRecord(
                domain="example.com",
                name="cdn",
                value="198.51.100.10",
                proxied=True,
            ),
            "tester",
        )
        return control, repository

    return build


@pytest.fixture
def certificate_pair():
    def generate(
        domains: tuple[str, ...] = ("cdn.example.com",),
        *,
        valid: bool = True,
        days: int = 30,
    ) -> tuple[bytes, bytes]:
        """``days`` is the remaining lifetime, for exercising expiry logic."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        start = now - timedelta(days=1) if valid else now - timedelta(days=10)
        end = now + timedelta(days=days) if valid else now - timedelta(days=1)
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
