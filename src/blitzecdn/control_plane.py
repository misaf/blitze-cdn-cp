"""The composition root.

This is the one module that knows both halves: it builds the concrete adapters
from :mod:`blitzecdn.infrastructure` and injects them into the pure services in
:mod:`blitzecdn.application`. Production wiring lives here and nowhere else, so
"what does a real control plane consist of" is answerable by reading one
constructor.

``ControlPlane`` is that constructor and nothing else. It holds the four
services and the ports the entry layers read through, and it forwards no calls:
the CLI and the API reach the service that owns the work —
``control_plane.dns.create_record(...)`` — rather than a method here that would
restate a signature already written on the service.

Everything reachable from here is a service or a port. The concrete
``Repository`` is deliberately not an attribute: an entry layer that could
reach it would be one import away from calling SQLite directly, and the read
endpoints that used to do exactly that are the reason this rule is written down
rather than assumed.
"""

from __future__ import annotations

from blitzecdn.application import (
    CertificateService,
    DeploymentService,
    DnsService,
    EdgeOperationsService,
)
from blitzecdn.config import Settings
from blitzecdn.infrastructure.ansible import AnsibleRunner
from blitzecdn.infrastructure.certificates import CertbotIssuer, CertificateStore
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.events import AuditObserver, InProcessEventBus
from blitzecdn.infrastructure.filesystem import atomic_write_yaml, read_log_tail
from blitzecdn.infrastructure.origins import OriginProbe
from blitzecdn.infrastructure.preflight import CertificatePreflight
from blitzecdn.infrastructure.process import ThreadBackgroundRunner
from blitzecdn.ports import (
    AuditTrail,
    BackgroundRunner,
    DeploymentRunner,
    Issuer,
    Preflight,
)
from blitzecdn.ports import (
    CertificateStore as CertificateStorePort,
)
from blitzecdn.ports import (
    EdgeStore as EdgeStorePort,
)
from blitzecdn.ports import (
    OriginProbe as OriginProbePort,
)


class ControlPlane:
    """The wired services, and the ports the entry layers read through."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository | None = None,
        runner: DeploymentRunner | None = None,
        certificate_store: CertificateStorePort | None = None,
        issuer: Issuer | None = None,
        origin_probe: OriginProbePort | None = None,
        preflight: Preflight | None = None,
        edges_store: EdgeStorePort | None = None,
        background: BackgroundRunner | None = None,
    ) -> None:
        self.settings = settings
        store = repository or Repository(settings.database_path)
        # The fleet, and the rows the `blitzecdn` Ansible inventory plugin reads
        # for itself at the start of every run. Both the runner and preflight
        # take it so that "which edges exist" has exactly one answer, whoever is
        # asking and whichever process they are in.
        self.edges_store = edges_store or store.edges
        self.runner = runner or AnsibleRunner(settings, self.edges_store)
        self.certificate_store = certificate_store or CertificateStore(settings)
        self.issuer = issuer or CertbotIssuer(settings)
        self.origin_probe = origin_probe or OriginProbe(settings)
        self.preflight = preflight or CertificatePreflight(
            settings, self.edges_store, origin_probe=self.origin_probe
        )
        self.background = background or ThreadBackgroundRunner()

        # The audit trail as a read-only port. It is written by the observer
        # below, never by a caller, so nothing here can record an event no
        # service actually announced.
        self.audit: AuditTrail = store.audit_log

        # The one subscriber the audit trail needs is registered here, so the
        # services can publish "something happened" without knowing who listens.
        self.bus = InProcessEventBus()
        self.bus.subscribe(AuditObserver(store.audit_log))

        # Each store is passed where its port is asked for, so a service is
        # handed the slice of persistence it declared and no more.
        self.dns = DnsService(store.zones, store.sites, self.bus)
        self.deployments = DeploymentService(
            settings,
            store.deployments,
            store.zones,
            self.bus,
            self.runner,
            self.certificate_store,
            self.dns,
            self.background,
            atomic_write_yaml,
            read_log_tail,
        )
        self.certificates = CertificateService(
            settings,
            store.sites,
            self.bus,
            self.runner,
            self.certificate_store,
            self.issuer,
            self.preflight,
            self.dns,
            self.deployments,
        )
        self.edges = EdgeOperationsService(
            settings,
            store.sites,
            self.bus,
            self.runner,
            self.origin_probe,
            self.edges_store,
        )


def build_control_plane(settings: Settings) -> ControlPlane:
    """Build a control plane wired to the real adapters."""
    return ControlPlane(settings)
