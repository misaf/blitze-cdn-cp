"""Issuing, uploading, renewing and reporting on TLS certificates.

Certificate state is written to the zone or rule that produced a host, through
``SiteEditing.activate_managed_certificate``; the host itself is derived.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from blitzecdn.capabilities.deployments.domain import DeploymentRequirementKind
from blitzecdn.capabilities.tls.certificates.domain import (
    CERTIFICATE_RENEWAL_DAYS,
    CERTIFICATE_WORKFLOW,
    CertificateInfo,
    CertificateSource,
    CertificateStatus,
    PreflightReport,
    ReconciliationResult,
    RenewalResult,
)
from blitzecdn.capabilities.tls.certificates.ports import (
    CertificateStore,
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
    EventRecorder,
    Issuer,
    MaterialReferences,
    Preflight,
    RecordReader,
    SiteEditor,
    SiteReader,
    UnitOfWork,
    WorkflowCoordinator,
    WorkflowProgress,
)
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    CertificateVersion,
    TlsPolicy,
)
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import (
    BlitzeError,
    ConflictError,
    DeploymentBusyError,
    NotFoundError,
)
from blitzecdn.core.ports.clock import Clock, system_clock

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CertificatePolicy:
    """Configuration owned by certificate workflows."""

    default_email: str | None


@dataclass(frozen=True)
class CertificatePersistence:
    """Certificate and site state committed in the same workflows."""

    sites: SiteReader
    certificates: CertificateStore
    uow: UnitOfWork
    requirements: DeploymentRequirements


@dataclass(frozen=True)
class CertificateExecution:
    """External capabilities used while managing certificates."""

    runner: DeploymentLocker
    issuer: Issuer
    preflight: Preflight


class CertificateService:
    """Everything that talks to a CA or to the certificate store."""

    def __init__(
        self,
        *,
        material_references: MaterialReferences,
        policy: CertificatePolicy,
        persistence: CertificatePersistence,
        execution: CertificateExecution,
        events: EventRecorder,
        dns: RecordReader,
        site_editor: SiteEditor,
        deployments: DeploymentGateway,
        workflows: WorkflowCoordinator,
        clock: Clock = system_clock,
    ) -> None:
        self.clock = clock
        self.material_references = material_references
        self.policy = policy
        self.persistence = persistence
        self.execution = execution
        self.events = events
        self.dns = dns
        self.site_editor = site_editor
        self.deployments = deployments
        self.workflows = workflows

    # -- Installing ----------------------------------------------------

    def upload_certificate(
        self,
        name: str,
        certificate_pem: bytes,
        private_key_pem: bytes,
        operator: str,
    ) -> CertificateInfo:
        with (
            self.execution.runner.lock(),
            self.workflows.run(CERTIFICATE_WORKFLOW, operator, name) as progress,
        ):
            site = self.persistence.sites.get_site(name)
            info = self.persistence.certificates.install(
                site,
                certificate_pem,
                private_key_pem,
                source=CertificateSource.UPLOADED,
            )
            progress.checkpoint("installed", {"site": name})
            with self.persistence.uow.transaction():
                self.site_editor.activate_managed_certificate(
                    site, CertificateMode.UPLOADED, info.version
                )
                self.persistence.requirements.require(
                    DeploymentRequirementKind.CERTIFICATES
                )
                self.events.record(
                    domain_event(
                        operator,
                        "certificate.uploaded",
                        "site",
                        name,
                        {
                            "domains": list(info.domains),
                            "not_after": info.not_after.isoformat(),
                        },
                    )
                )
            progress.checkpoint("activated", {"site": name})
        return info

    # -- Preflight -----------------------------------------------------

    def certificate_preflight(self, name: str) -> PreflightReport:
        """Report whether HTTP-01 could validate this site right now, without the CA."""
        site = self.persistence.sites.get_site(name)
        record = self.dns.record_for_hostname(site.server_names[0])
        return self.execution.preflight.check(
            site,
            deployed=self.deployments.site_is_deployed(site.name),
            record_ttl=record.ttl,
        )

    def _enforce_preflight(
        self, site: CdnSite, operator: str, *, skip: bool, action: str
    ) -> PreflightReport:
        """Refuse issuance that cannot succeed; audit an override as its own event."""
        report = self.execution.preflight.check(
            site,
            deployed=self.deployments.site_is_deployed(site.name),
            record_ttl=self.dns.record_for_hostname(site.server_names[0]).ttl,
        )
        for advisory in report.advisories:
            _LOGGER.warning(
                "certificate preflight advisory for %s: %s: %s",
                site.name,
                advisory.name,
                advisory.detail,
            )
        if report.ok:
            return report
        if not skip:
            raise ConflictError(
                f"certificate preflight failed for {site.name}: {report.summary()}. "
                "Fix the above, or pass skip_preflight to issue anyway."
            )
        self.events.record(
            domain_event(
                operator,
                f"{action}.preflight_overridden",
                "site",
                site.name,
                {
                    "failures": [
                        {"check": check.name, "detail": check.detail}
                        for check in report.blocking_failures
                    ]
                },
            )
        )
        _LOGGER.warning(
            "certificate preflight overridden for %s: %s",
            site.name,
            report.summary(),
        )
        return report

    # -- Issuing -------------------------------------------------------

    def request_certificate(
        self,
        name: str,
        operator: str,
        email: str | None = None,
        *,
        skip_preflight: bool = False,
    ) -> CertificateInfo:
        registration_email = email or self.policy.default_email
        if not registration_email:
            raise ConflictError(
                "provide an email or configure BLITZE_ACME_DEFAULT_EMAIL"
            )
        with self.execution.runner.lock():
            with self.workflows.run(CERTIFICATE_WORKFLOW, operator, name) as progress:
                site = self.persistence.sites.get_site(name)
                info = self._issue_certificate_locked(
                    site,
                    operator,
                    registration_email,
                    skip_preflight=skip_preflight,
                    progress=progress,
                )
                progress.checkpoint("activated", {"site": name})
            return info

    def _issue_certificate_locked(
        self,
        site: CdnSite,
        operator: str,
        registration_email: str,
        *,
        skip_preflight: bool = False,
        progress: WorkflowProgress | None = None,
    ) -> CertificateInfo:
        """Issue and activate one certificate while the caller holds the lock.

        Checkpointed per step, so recovery can tell a certificate the CA issued but we
        never stored (a spent rate limit) from one stored but not yet activated (fixed
        by the next issuance).
        """
        self._enforce_preflight(
            site, operator, skip=skip_preflight, action="certificate.requested"
        )
        certificate_pem, private_key_pem = self.execution.issuer.issue(
            site, registration_email
        )
        if progress is not None:
            progress.checkpoint("issued", {"site": site.name})
        info = self.persistence.certificates.install(
            site,
            certificate_pem,
            private_key_pem,
            source=CertificateSource.ACME,
            email=registration_email,
        )
        if progress is not None:
            progress.checkpoint("stored", {"site": site.name})
        with self.persistence.uow.transaction():
            self.site_editor.activate_managed_certificate(
                site, CertificateMode.REQUESTED, info.version
            )
            self.persistence.requirements.require(
                DeploymentRequirementKind.CERTIFICATES
            )
            self.events.record(
                domain_event(
                    operator,
                    "certificate.requested",
                    "site",
                    site.name,
                    {
                        "domains": list(info.domains),
                        "not_after": info.not_after.isoformat(),
                    },
                )
            )
        return info

    def reconcile_certificates(self, operator: str) -> ReconciliationResult:
        """Issue ready first certificates and queue one deployment to install them.

        Queued through deployment requirements, so a second run queues nothing extra.
        Eligibility is re-checked under the cross-process lock, so only one reconciler
        contacts the CA for a site.
        """
        issued: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        for candidate in self.persistence.sites.list_sites():
            if (
                candidate.policy[TlsPolicy].certificate_mode
                is not CertificateMode.DISABLED
            ):
                continue
            report = self.certificate_preflight(candidate.name)
            if not report.ok:
                skipped[candidate.name] = report.summary()
                continue
            try:
                # Journalled like every path to the CA; this one is unattended.
                with (
                    self.execution.runner.lock(),
                    self.workflows.run(
                        CERTIFICATE_WORKFLOW, operator, candidate.name
                    ) as progress,
                ):
                    site = self.persistence.sites.get_site(candidate.name)
                    if (
                        site.policy[TlsPolicy].certificate_mode
                        is not CertificateMode.DISABLED
                    ):
                        continue
                    registration_email = self.policy.default_email
                    if not registration_email:
                        raise ConflictError(
                            "configure BLITZE_ACME_DEFAULT_EMAIL for unattended "
                            "issuance"
                        )
                    self._issue_certificate_locked(
                        site,
                        operator,
                        registration_email,
                        skip_preflight=False,
                        progress=progress,
                    )
                    progress.checkpoint("activated", {"site": site.name})
                issued.append(site.name)
            except BlitzeError as exc:
                failed[candidate.name] = str(exc)

        deployment = (
            self.deployments.converge_requirements(operator) if issued else None
        )
        return ReconciliationResult(
            issued=tuple(issued),
            skipped=skipped,
            failed=failed,
            deployment=deployment,
        )

    # -- Reporting -----------------------------------------------------

    def certificate(self, name: str) -> CertificateInfo:
        self.persistence.sites.get_site(name)
        return self.persistence.certificates.get(name)

    def installed_sources(self, site: CdnSite) -> tuple[Path, Path]:
        """Verify and locate the exact version captured policy selects."""
        return self.persistence.certificates.sources(
            site.name,
            version=CertificateVersion.from_paths(
                site.name,
                site.policy[TlsPolicy].certificate_path or "",
                site.policy[TlsPolicy].certificate_key_path or "",
            ),
        )

    def collect_material(self, operator: str) -> tuple[str, ...]:
        """Collect superseded files, fenced against issuance; safe to retry."""
        with self.execution.runner.lock(), self.persistence.uow.transaction():
            removed = self.persistence.certificates.collect(self.material_references())
            self.events.record(
                domain_event(
                    operator,
                    "certificate.material.collected",
                    "certificates",
                    "controller",
                    {"removed": list(removed)},
                )
            )
            return removed

    def certificate_statuses(self) -> list[CertificateStatus]:
        """Every managed certificate against the clock, soonest expiry first."""
        now = self.clock()
        return [
            CertificateStatus.of(info, now=now)
            for info in self.persistence.certificates.list_all()
        ]

    def expiring_certificates(
        self, within_days: int = CERTIFICATE_RENEWAL_DAYS
    ) -> list[CertificateStatus]:
        """Certificates close to expiry, uploaded ones (unrenewable) included."""
        return [
            status
            for status in self.certificate_statuses()
            if status.days_remaining <= within_days
        ]

    # -- Renewal -------------------------------------------------------

    def renew_certificates(
        self,
        operator: str,
        *,
        within_days: int = CERTIFICATE_RENEWAL_DAYS,
        force: bool = False,
        sites: Sequence[str] | None = None,
        budget_seconds: float | None = None,
    ) -> RenewalResult:
        """Reissue ACME certificates that are close to expiry.

        Every site is attempted even if one fails; the next run picks up the rest.
        ``sites`` narrows the run, and an unknown name is an error, not a no-op.
        Nothing is deployed here. Each renewal goes through preflight with no override,
        so a timer can never force past a failed check. ``budget_seconds`` is checked
        only between sites, never mid-issuance; unreached sites are reported skipped.
        """
        renewed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        deadline = (
            None if budget_seconds is None else monotonic() + max(budget_seconds, 0.0)
        )
        candidates, deferred = self._renewal_candidates(
            within_days=within_days, force=force, sites=sites
        )
        skipped.extend(deferred)
        for current, status in candidates:
            if deadline is not None and monotonic() >= deadline:
                skipped.append(
                    f"{status.site}: not attempted, the renewal budget of "
                    f"{budget_seconds:.0f}s was spent on earlier sites. It will "
                    "be retried by the next run."
                )
                continue
            outcome, message = self._renew_one(current, status, operator)
            match outcome:
                case "renewed":
                    renewed.append(message)
                case "skipped":
                    skipped.append(message)
                case "failed":
                    failed.append(message)
        self.events.record(
            domain_event(
                operator,
                "certificates.renewed",
                "site",
                None,
                {
                    "renewed": renewed,
                    "skipped": skipped,
                    "failed": failed,
                    "within_days": within_days,
                    # Records whether the sweep was narrowed.
                    "sites": None if sites is None else sorted(sites),
                },
            )
        )
        return RenewalResult(
            renewed=tuple(renewed), skipped=tuple(skipped), failed=tuple(failed)
        )

    def _renewal_candidates(
        self,
        *,
        within_days: int,
        force: bool,
        sites: Sequence[str] | None,
    ) -> tuple[list[tuple[CertificateInfo, CertificateStatus]], list[str]]:
        """Select renewable certificates and explain actionable exclusions."""
        stored = self.persistence.certificates.list_all()
        selected = None if sites is None else set(sites)
        if selected is not None:
            unknown = sorted(selected - {info.site for info in stored})
            if unknown:
                raise NotFoundError("no managed certificate for: " + ", ".join(unknown))

        candidates: list[tuple[CertificateInfo, CertificateStatus]] = []
        skipped: list[str] = []
        now = self.clock()
        for current in stored:
            if selected is not None and current.site not in selected:
                continue
            status = CertificateStatus.of(current, now=now)
            if not status.renewable:
                if status.days_remaining <= within_days:
                    skipped.append(
                        f"{status.site}: expires in {status.days_remaining} day(s) "
                        "but was uploaded, not issued by BlitzeCDN. Ask whoever "
                        "supplied it for a replacement and upload that."
                    )
                continue
            if force or status.due_for_renewal(within_days):
                candidates.append((current, status))
        return candidates, skipped

    def _renew_one(
        self,
        current: CertificateInfo,
        status: CertificateStatus,
        operator: str,
    ) -> tuple[Literal["renewed", "skipped", "failed"], str]:
        """Attempt one renewal and classify its operator-facing result."""
        try:
            # Preserve the subscription's ACME account when the default changes.
            info = self.request_certificate(status.site, operator, current.email)
        except DeploymentBusyError:
            _LOGGER.info("renewal deferred for %s: deployment in progress", status.site)
            return (
                "skipped",
                f"{status.site}: not attempted, a deployment was running. It will "
                "be retried by the next run.",
            )
        except BlitzeError as exc:
            _LOGGER.warning("renewal failed for %s: %s", status.site, exc)
            return "failed", f"{status.site}: {exc}"
        if info.fingerprint_sha256 == current.fingerprint_sha256:
            # Renewal returning the certificate already in place is a failure, however
            # cleanly the issuer exited: nothing new deploys and the expiry stands.
            _LOGGER.error(
                "renewal of %s returned the certificate already in place (%s)",
                status.site,
                current.fingerprint_sha256,
            )
            return (
                "failed",
                f"{status.site}: the issuer returned the certificate already in "
                f"place, still expiring {current.not_after:%Y-%m-%d}. Nothing was "
                "renewed.",
            )
        _LOGGER.info("renewed %s, now valid until %s", status.site, info.not_after)
        return "renewed", status.site
