"""Issuing, uploading, renewing and reporting on TLS certificates.

Certificate state is written to the *record*, through
``DnsService.activate_managed_certificate`` — writing it to the derived sites
table would survive only until the next record change re-derived over it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from time import monotonic

from blitzecdn.config import Settings
from blitzecdn.domain.certificates import (
    CERTIFICATE_RENEWAL_DAYS,
    CertificateInfo,
    CertificateSource,
    CertificateStatus,
    PreflightReport,
)
from blitzecdn.domain.events import domain_event
from blitzecdn.domain.sites import CdnSite, CertificateMode
from blitzecdn.exceptions import BlitzeError, ConflictError, NotFoundError
from blitzecdn.ports import (
    CertificateStore,
    DeploymentGateway,
    DeploymentRunner,
    EventBus,
    Issuer,
    Preflight,
    SiteStore,
    ZoneEditor,
)

_LOGGER = logging.getLogger(__name__)


class CertificateService:
    """Everything that talks to a CA or to the certificate store."""

    def __init__(
        self,
        settings: Settings,
        sites: SiteStore,
        bus: EventBus,
        runner: DeploymentRunner,
        certificate_store: CertificateStore,
        issuer: Issuer,
        preflight: Preflight,
        dns: ZoneEditor,
        deployments: DeploymentGateway,
    ) -> None:
        self.settings = settings
        self.sites = sites
        self.bus = bus
        self.runner = runner
        self.certificate_store = certificate_store
        self.issuer = issuer
        self.preflight = preflight
        self.dns = dns
        self.deployments = deployments

    # -- Installing ----------------------------------------------------

    def upload_certificate(
        self,
        name: str,
        certificate_pem: bytes,
        private_key_pem: bytes,
        operator: str,
    ) -> CertificateInfo:
        with self.runner.lock():
            site = self.sites.get_site(name)
            info = self.certificate_store.install(
                site,
                certificate_pem,
                private_key_pem,
                source=CertificateSource.UPLOADED,
            )
            self.dns.activate_managed_certificate(site, CertificateMode.UPLOADED)
        self.bus.publish(
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
        return info

    # -- Preflight -----------------------------------------------------

    def certificate_preflight(self, name: str) -> PreflightReport:
        """Report whether HTTP-01 could validate this site right now.

        Public so an operator can ask before spending a rate-limited request,
        and so the answer to "why did issuance fail" does not require reading
        certbot's output.
        """
        site = self.sites.get_site(name)
        record = self.dns.record_for_site(site.name)
        return self.preflight.check(
            site,
            deployed=self.deployments.site_is_deployed(site.name),
            record_ttl=record.ttl,
        )

    def _enforce_preflight(
        self, site: CdnSite, operator: str, *, skip: bool, action: str
    ) -> PreflightReport:
        """Refuse issuance that cannot succeed, and record it either way.

        An override is audited as its own event rather than a field on the
        success record: "who forced past a failed check" is a question asked
        after an incident, and it should not require reading every certificate
        event to answer.
        """
        report = self.preflight.check(
            site,
            deployed=self.deployments.site_is_deployed(site.name),
            record_ttl=self.dns.record_for_site(site.name).ttl,
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
        self.bus.publish(
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
        registration_email = email or self.settings.acme_default_email
        if not registration_email:
            raise ConflictError(
                "provide an email or configure BLITZE_ACME_DEFAULT_EMAIL"
            )
        with self.runner.lock():
            site = self.sites.get_site(name)
            info = self._issue_certificate_locked(
                site,
                operator,
                registration_email,
                skip_preflight=skip_preflight,
            )
        return info

    def _issue_certificate_locked(
        self,
        site: CdnSite,
        operator: str,
        registration_email: str,
        *,
        skip_preflight: bool = False,
    ) -> CertificateInfo:
        """Issue and activate one certificate while the caller holds the lock."""
        self._enforce_preflight(
            site, operator, skip=skip_preflight, action="certificate.requested"
        )
        certificate_pem, private_key_pem = self.issuer.issue(site, registration_email)
        info = self.certificate_store.install(
            site,
            certificate_pem,
            private_key_pem,
            source=CertificateSource.ACME,
            email=registration_email,
        )
        self.dns.activate_managed_certificate(site, CertificateMode.REQUESTED)
        self.bus.publish(
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

    def reconcile_certificates(self, operator: str) -> dict[str, object]:
        """Issue ready first certificates and install them with one deployment.

        Eligibility is checked again after taking the cross-process lock. Two
        reconcilers may discover the same site, but only the first can still see
        it in disabled mode and contact the CA.
        """
        issued: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        for candidate in self.sites.list_sites():
            if candidate.certificate_mode is not CertificateMode.DISABLED:
                continue
            report = self.certificate_preflight(candidate.name)
            if not report.ok:
                skipped[candidate.name] = report.summary()
                continue
            try:
                with self.runner.lock():
                    site = self.sites.get_site(candidate.name)
                    if site.certificate_mode is not CertificateMode.DISABLED:
                        continue
                    registration_email = self.settings.acme_default_email
                    if not registration_email:
                        raise ConflictError(
                            "configure BLITZE_ACME_DEFAULT_EMAIL for unattended "
                            "issuance"
                        )
                    self._issue_certificate_locked(
                        site, operator, registration_email, skip_preflight=False
                    )
                issued.append(site.name)
            except BlitzeError as exc:
                failed[candidate.name] = str(exc)

        deployment = self.deployments.deploy(operator) if issued else None
        return {
            "issued": issued,
            "skipped": skipped,
            "failed": failed,
            "deployment": deployment,
        }

    # -- Reporting -----------------------------------------------------

    def certificate(self, name: str) -> CertificateInfo:
        self.sites.get_site(name)
        return self.certificate_store.get(name)

    def certificate_statuses(self) -> list[CertificateStatus]:
        """Every managed certificate against the clock, soonest expiry first."""
        now = datetime.now(UTC)
        return [
            CertificateStatus.of(info, now=now)
            for info in self.certificate_store.list_all()
        ]

    def expiring_certificates(
        self, within_days: int = CERTIFICATE_RENEWAL_DAYS
    ) -> list[CertificateStatus]:
        """Certificates close enough to expiry to need action.

        Includes uploaded ones, which BlitzeCDN cannot renew for itself. They
        are precisely the ones worth surfacing early — someone has to be asked
        for a replacement, and that takes longer than an ACME round trip.
        """
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
    ) -> dict[str, list[str]]:
        """Reissue ACME certificates that are close to expiry.

        Every certificate is attempted even if an earlier one fails. Renewal
        goes over the network to a CA and through an HTTP-01 challenge served
        by the edges, so one site failing is an ordinary transient event and
        must not stop the others from being renewed — the whole point of
        running this on a schedule is that the next run picks up what this one
        could not finish.

        ``sites`` narrows the run to the named sites, for retrying one failure
        without putting every other subscription through the CA again. An
        unknown name is an error rather than a silent no-op: the caller asked
        for a specific certificate, and reporting "nothing to renew" for a
        typo is how an expiry gets missed.

        Nothing is deployed here. A renewed certificate is installed into the
        control plane's own store and reaches the edges on the next deploy,
        which stays an explicit act.

        Each renewal goes through ``request_certificate`` and therefore through
        preflight. A hostname whose DNS has been moved off the edges fails here
        with the reason named, instead of consuming a CA request and reporting
        certbot's output. There is deliberately no override on this path: an
        unattended timer must not be able to force past a failed check.

        ``budget_seconds`` bounds the whole sweep for a caller that cannot wait
        indefinitely — the API passes one, the CLI and the timer do not. It is
        checked only between sites, never during one: abandoning a request
        mid-issuance would leave the CA's view and ours disagreeing about a
        certificate that may well have been issued. A site the budget did not
        reach is reported as skipped and renewed by the next run, which is
        exactly what a site that failed transiently already does.
        """
        renewed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        now = datetime.now(UTC)
        deadline = (
            None if budget_seconds is None else monotonic() + max(budget_seconds, 0.0)
        )
        stored = self.certificate_store.list_all()
        selected = None if sites is None else set(sites)
        if selected is not None:
            unknown = sorted(selected - {info.site for info in stored})
            if unknown:
                raise NotFoundError("no managed certificate for: " + ", ".join(unknown))
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
            if not force and not status.due_for_renewal(within_days):
                continue
            if deadline is not None and monotonic() >= deadline:
                skipped.append(
                    f"{status.site}: not attempted, the renewal budget of "
                    f"{budget_seconds:.0f}s was spent on earlier sites. It will "
                    "be retried by the next run."
                )
                continue
            try:
                # Renew under the address the certificate was registered with,
                # so a changed default cannot silently move an existing
                # subscription to a different ACME account.
                info = self.request_certificate(status.site, operator, current.email)
            except BlitzeError as exc:
                failed.append(f"{status.site}: {exc}")
                _LOGGER.warning("renewal failed for %s: %s", status.site, exc)
                continue
            renewed.append(status.site)
            _LOGGER.info("renewed %s, now valid until %s", status.site, info.not_after)
        self.bus.publish(
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
                    # Without this the audit trail cannot tell a full sweep that
                    # renewed one certificate from a narrowed run that only
                    # looked at one, which is the question asked after a missed
                    # expiry.
                    "sites": None if sites is None else sorted(sites),
                },
            )
        )
        return {"renewed": renewed, "skipped": skipped, "failed": failed}
