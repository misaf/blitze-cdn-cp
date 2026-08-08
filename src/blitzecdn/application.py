from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from blitzecdn.config import Settings
from blitzecdn.domain.models import (
    CERTIFICATE_RENEWAL_DAYS,
    DESIRED_STATE_VERSION,
    CdnSite,
    CertificateInfo,
    CertificateMode,
    CertificateSource,
    CertificateStatus,
    Deployment,
    DeploymentStatus,
    DnsRecord,
    Domain,
    DriftReport,
    OriginCheck,
    RecordPatch,
    RecordType,
    managed_certificate_paths,
    validate_edge_limit,
)
from blitzecdn.exceptions import (
    BlitzeError,
    ConflictError,
    ExecutionError,
    NotFoundError,
)
from blitzecdn.infrastructure.ansible import AnsibleRunner, parse_play_recap
from blitzecdn.infrastructure.certificates import (
    CertbotIssuer,
    CertificateStore,
    Issuer,
)
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.filesystem import atomic_write_yaml
from blitzecdn.infrastructure.origins import OriginProbe

_LOGGER = logging.getLogger(__name__)


class ControlPlane:
    """Coordinate persistence and Ansible at the application boundary."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository | None = None,
        runner: AnsibleRunner | None = None,
        certificate_store: CertificateStore | None = None,
        issuer: Issuer | None = None,
        origin_probe: OriginProbe | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or Repository(settings.database_path)
        self.runner = runner or AnsibleRunner(settings)
        self.certificate_store = certificate_store or CertificateStore(settings)
        self.issuer = issuer or CertbotIssuer(settings)
        self.origin_probe = origin_probe or OriginProbe(settings)

    def initialize(self) -> int:
        return self.repository.abandon_running()

    # ------------------------------------------------------------------
    # Domains and records
    #
    # Records are the source of truth. Sites are derived from them and
    # rewritten by _sync_sites() after every change, so nothing should write
    # to the sites table directly — an edit made there survives only until the
    # next record change silently reverts it.
    # ------------------------------------------------------------------

    def list_domains(self) -> list[Domain]:
        return self.repository.list_domains()

    def create_domain(self, domain: Domain, operator: str) -> Domain:
        created = self.repository.create_domain(domain)
        self.repository.audit(operator, "domain.created", "domain", domain.name)
        return created

    def delete_domain(self, name: str, operator: str) -> None:
        """Remove a zone and every record in it.

        The records go by cascade, so their virtual hosts have to come off the
        edge in the same breath — hence the re-derivation before returning.
        """
        self.repository.delete_domain(name)
        self._sync_sites()
        self.repository.audit(operator, "domain.deleted", "domain", name)

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        if domain is not None:
            self.repository.get_domain(domain)
        return self.repository.list_records(domain)

    def _reject_derived_name_collision(self, record: DnsRecord) -> None:
        """Refuse a record whose site name another record already derives.

        Two hostnames can flatten to one internal site name — ``a.b.example.com``
        and ``a-b.example.com`` both give ``a-b-example-com``. Caught here the
        operator gets a clear conflict on the record they just typed; left to
        the derived write it surfaces as a UNIQUE constraint from SQLite.
        """
        if not record.proxied:
            return
        for existing in self.repository.list_records():
            if not existing.proxied or existing.fqdn == record.fqdn:
                continue
            if existing.site_name == record.site_name:
                raise ConflictError(
                    f"{record.fqdn!r} and {existing.fqdn!r} both map to the "
                    f"internal site name {record.site_name!r}. Rename one."
                )

    def create_record(self, record: DnsRecord, operator: str) -> DnsRecord:
        self._reject_derived_name_collision(record)
        created = self.repository.create_record(record)
        self._sync_sites()
        self.repository.audit(
            operator,
            "record.created",
            "record",
            created.fqdn,
            {"type": created.type.value, "proxied": created.proxied},
        )
        return created

    def update_record(
        self,
        domain: str,
        name: str,
        type_: RecordType,
        patch: RecordPatch,
        operator: str,
    ) -> DnsRecord:
        current = self.repository.get_record(domain, name, type_)
        updated = DnsRecord.model_validate(
            {**current.model_dump(), **patch.model_dump(exclude_unset=True)}
        )
        self._reject_derived_name_collision(updated)
        saved = self.repository.replace_record(updated)
        self._sync_sites()
        self.repository.audit(
            operator,
            "record.updated",
            "record",
            saved.fqdn,
            {"fields": sorted(patch.model_fields_set)},
        )
        return saved

    def set_proxied(
        self, domain: str, name: str, type_: RecordType, proxied: bool, operator: str
    ) -> DnsRecord:
        """Turn the CDN on or off for one record.

        Only half the switch. The edge stops or starts serving the hostname on
        the next deploy, but the record only reaches clients once DNS answers
        with the matching address — the edge's for a proxied record, ``value``
        for an unproxied one. Until DNS agrees, an unproxied hostname still
        pointed at an edge gets the catch-all's 444.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=proxied), operator
        )

    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None:
        record = self.repository.get_record(domain, name, type_)
        self.repository.delete_record(domain, name, type_)
        self._sync_sites()
        self.repository.audit(operator, "record.deleted", "record", record.fqdn)

    def _sync_sites(self) -> None:
        """Rewrite the derived sites table from the records that produce it."""
        self.repository.replace_all_sites(self._derive_sites())

    def _derive_sites(self) -> list[CdnSite]:
        """Derive the virtual hosts the edge should serve.

        Deduplicated by name rather than trusting the collision check in
        ``_reject_derived_name_collision``: records can also arrive from a
        restored snapshot, and a duplicate there must not turn into a SQLite
        integrity error deep inside a rollback. ``validate()`` still reports it.
        """
        sites: dict[str, CdnSite] = {}
        for record in self.repository.list_records():
            site = record.to_site()
            if site is not None:
                sites.setdefault(site.name, site)
        return list(sites.values())

    def dns_export(self) -> list[dict[str, object]]:
        """Every record, for the system that publishes DNS.

        A proxied record deliberately carries no address: it must resolve to an
        edge, and edge addressing belongs to the DNS system rather than here.
        ``value`` is still reported as ``origin`` so the two can be reconciled.
        """
        return [
            {
                "fqdn": record.fqdn,
                "domain": record.domain,
                "name": record.name,
                "type": record.type.value,
                "ttl": record.ttl,
                "proxied": record.proxied,
                "origin": record.value,
                **({} if record.proxied else {"value": record.value}),
            }
            for record in self.repository.list_records()
        ]

    def upload_certificate(
        self,
        name: str,
        certificate_pem: bytes,
        private_key_pem: bytes,
        operator: str,
    ) -> CertificateInfo:
        with self.runner.lock():
            site = self.repository.get_site(name)
            info = self.certificate_store.install(
                site,
                certificate_pem,
                private_key_pem,
                source=CertificateSource.UPLOADED,
            )
            self._activate_managed_certificate(site, CertificateMode.UPLOADED)
        self.repository.audit(
            operator,
            "certificate.uploaded",
            "site",
            name,
            {"domains": list(info.domains), "not_after": info.not_after.isoformat()},
        )
        return info

    def request_certificate(
        self, name: str, operator: str, email: str | None = None
    ) -> CertificateInfo:
        registration_email = email or self.settings.acme_default_email
        if not registration_email:
            raise ConflictError(
                "provide an email or configure BLITZE_ACME_DEFAULT_EMAIL"
            )
        with self.runner.lock():
            site = self.repository.get_site(name)
            certificate_pem, private_key_pem = self.issuer.issue(
                site, registration_email
            )
            info = self.certificate_store.install(
                site,
                certificate_pem,
                private_key_pem,
                source=CertificateSource.ACME,
                email=registration_email,
            )
            self._activate_managed_certificate(site, CertificateMode.REQUESTED)
        self.repository.audit(
            operator,
            "certificate.requested",
            "site",
            name,
            {"domains": list(info.domains), "not_after": info.not_after.isoformat()},
        )
        return info

    def certificate(self, name: str) -> CertificateInfo:
        self.repository.get_site(name)
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

    def renew_certificates(
        self,
        operator: str,
        *,
        within_days: int = CERTIFICATE_RENEWAL_DAYS,
        force: bool = False,
    ) -> dict[str, list[str]]:
        """Reissue ACME certificates that are close to expiry.

        Every certificate is attempted even if an earlier one fails. Renewal
        goes over the network to a CA and through an HTTP-01 challenge served
        by the edges, so one site failing is an ordinary transient event and
        must not stop the others from being renewed — the whole point of
        running this on a schedule is that the next run picks up what this one
        could not finish.

        Nothing is deployed here. A renewed certificate is installed into the
        control plane's own store and reaches the edges on the next deploy,
        which stays an explicit act.
        """
        renewed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        now = datetime.now(UTC)
        for current in self.certificate_store.list_all():
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
        self.repository.audit(
            operator,
            "certificates.renewed",
            "site",
            None,
            {
                "renewed": renewed,
                "skipped": skipped,
                "failed": failed,
                "within_days": within_days,
            },
        )
        return {"renewed": renewed, "skipped": skipped, "failed": failed}

    def _record_for_site(self, site_name: str) -> DnsRecord:
        """Find the record a derived site came from.

        Certificate state has to land on the record: writing it to the sites
        table would survive only until the next record change re-derived over
        it.
        """
        for record in self.repository.list_records():
            if record.proxied and record.site_name == site_name:
                return record
        raise NotFoundError(
            f"no proxied DNS record produces site {site_name!r}. Certificates "
            "belong to a proxied record; enable the proxy first."
        )

    def _activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite:
        certificate_path, certificate_key_path = managed_certificate_paths(site.name)
        record = self._record_for_site(site.name)
        self.repository.replace_record(
            DnsRecord.model_validate(
                {
                    **record.model_dump(),
                    "certificate_mode": mode,
                    "certificate_path": certificate_path,
                    "certificate_key_path": certificate_key_path,
                }
            )
        )
        self._sync_sites()
        return self.repository.get_site(site.name)

    def check_origins(self) -> list[OriginCheck]:
        """Connect to every enabled site's origin the way the edge will.

        Deliberately not folded into ``validate()``, which ``deploy`` runs.
        Validation is about desired state being coherent and has to stay fast
        and deterministic; an origin being briefly unreachable is neither a
        reason to refuse a deploy of unrelated sites nor something a deploy
        should wait on. Run this before a deploy, not inside one.

        Disabled sites are skipped: the edge will not proxy to them, so their
        origins being down is not a fact about anything.
        """
        return self.origin_probe.check_all(
            [site for site in self.repository.list_sites() if site.enabled]
        )

    def validate(self) -> list[str]:
        errors = self.settings.validate_runtime()

        # Two records can flatten to one site name (a.b.example.com and
        # a-b.example.com both give a-b-example-com), which would silently make
        # one overwrite the other in the derived table.
        derived: dict[str, str] = {}
        for record in self.repository.list_records():
            if not record.proxied:
                continue
            previous = derived.setdefault(record.site_name, record.fqdn)
            if previous != record.fqdn:
                errors.append(
                    f"{record.fqdn!r} and {previous!r} both derive the internal "
                    f"site name {record.site_name!r}. Rename one of them."
                )
            if record.certificate_mode is CertificateMode.REQUESTED and (
                record.domain.endswith((".test", ".invalid", ".localhost", ".example"))
            ):
                errors.append(
                    f"{record.fqdn!r} requests an ACME certificate, but "
                    f"{record.domain!r} is a reserved name (RFC 6761/2606) that "
                    "no public CA will issue for. Upload a certificate instead."
                )

        names: dict[str, str] = {}
        for site in self.repository.list_sites():
            for server_name in site.server_names:
                previous = names.setdefault(server_name, site.name)
                if previous != site.name:
                    errors.append(
                        f"server name {server_name!r} belongs to both "
                        f"{previous!r} and {site.name!r}"
                    )
        if not errors:
            self._write_desired_state(self.repository.snapshot())
            result = self.runner.validate()
            if result.return_code != 0:
                errors.append(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "Ansible syntax validation failed"
                )
        return errors

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Converge the edges, returning once the run has finished.

        ``host_limit`` narrows the run to some of them — a canary. It is
        recorded on the deployment because it changes what success means: the
        snapshot became reality on the named edges only, and the rest are
        still serving whatever they had.
        """
        with self.runner.lock():
            return self._converge(
                self._queue(operator, check=check, host_limit=host_limit), operator
            )

    def submit_deployment(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        """Queue a convergence on a worker thread and return the queued record.

        A full run can take as long as ``deployment_timeout_seconds``, far
        longer than any HTTP client will wait, so callers poll
        ``GET /v1/deployments/{id}`` for the outcome.
        """
        return self._submit(
            lambda: self._queue(operator, check=check, host_limit=host_limit), operator
        )

    def check_drift(
        self, operator: str, *, host_limit: str | None = None
    ) -> DriftReport:
        """Ask the fleet whether it still matches the declared desired state.

        A check-mode convergence, read as a question rather than a rehearsal.
        Nothing on any edge changes; the run reports what it *would* change,
        and anything it would change is by definition something that drifted
        away from desired state since the last deploy.
        """
        deployment = self.deploy(operator, check=True, host_limit=host_limit)
        report = self.drift_report(deployment.id)
        self.repository.audit(
            operator,
            "drift.checked",
            "deployment",
            deployment.id,
            {
                "in_sync": report.in_sync,
                "drifted": [host.host for host in report.drifted],
                "unreachable": [host.host for host in report.unreachable],
            },
        )
        return report

    def drift_report(self, deployment_id: str) -> DriftReport:
        """Read a recorded check-mode run as a drift report.

        Derived from the stored deployment rather than only from a live run, so
        the CLI and the API share one interpretation and an operator can revisit
        the answer a scheduled check produced without re-running it.
        """
        deployment = self.repository.get_deployment(deployment_id)
        if not deployment.check_mode:
            raise ConflictError(
                f"deployment {deployment_id} applied changes rather than "
                "previewing them, so its output describes what it did, not "
                "what had drifted. Run 'blitzecdn drift' instead."
            )
        return DriftReport(
            deployment_id=deployment.id,
            checked_at=deployment.finished_at or deployment.created_at,
            host_limit=deployment.host_limit,
            hosts=parse_play_recap(deployment.stdout),
        )

    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Converge a prior snapshot and adopt it as canonical desired state.

        Deliberately takes no host limit. On success this rewrites the
        canonical records, so a rollback that reached only some edges would
        leave the control plane asserting a state the rest of the fleet has
        never been given — the precise disagreement rollback exists to end.
        """
        with self.runner.lock():
            return self._converge(
                self._queue_rollback(operator, deployment_id, check=check), operator
            )

    def submit_rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Queue a rollback on a worker thread and return the queued record."""
        return self._submit(
            lambda: self._queue_rollback(operator, deployment_id, check=check), operator
        )

    def _queue(
        self,
        operator: str,
        *,
        check: bool,
        snapshot: str | None = None,
        rollback_of: str | None = None,
        host_limit: str | None = None,
    ) -> Deployment:
        """Record a QUEUED deployment. Callers must hold the deployment lock."""
        # Normalised before it is stored, so the record shows what actually ran
        # rather than what was typed, and a malformed limit is refused before a
        # deployment row exists to explain.
        limit = validate_edge_limit(host_limit)
        deployment = self.repository.create_deployment(
            operator,
            check_mode=check,
            rollback_of=rollback_of,
            snapshot=snapshot,
            host_limit=limit,
        )
        self.repository.audit(
            operator,
            "deployment.queued",
            "deployment",
            deployment.id,
            {"check_mode": check, "rollback_of": rollback_of, "host_limit": limit},
        )
        return deployment

    def _queue_rollback(
        self, operator: str, deployment_id: str | None, *, check: bool
    ) -> Deployment:
        target = (
            self.repository.get_deployment(deployment_id)
            if deployment_id
            else self.repository.successful_rollback_target(self.repository.snapshot())
        )
        if target.check_mode or target.status is not DeploymentStatus.SUCCEEDED:
            raise ConflictError(
                "rollback target must be a successful applied deployment"
            )
        return self._queue(
            operator,
            check=check,
            snapshot=self.repository.deployment_snapshot(target.id),
            rollback_of=target.id,
        )

    def _submit(self, queue: Callable[[], Deployment], operator: str) -> Deployment:
        """Take the deployment lock now, hand it to a worker, return the record.

        The lock is an fcntl lock on an open file, so releasing it from the
        worker thread is equivalent to releasing it here.
        """
        lock = self.runner.lock()
        lock.__enter__()
        try:
            deployment = queue()
        except BaseException:
            lock.__exit__(None, None, None)
            raise

        def worker() -> None:
            try:
                self._converge(deployment, operator)
            except Exception:
                _LOGGER.exception("deployment %s failed", deployment.id)
            finally:
                lock.__exit__(None, None, None)

        threading.Thread(
            target=worker, name=f"blitzecdn-deploy-{deployment.id}", daemon=True
        ).start()
        return deployment

    def _converge(self, deployment: Deployment, operator: str) -> Deployment:
        """Run Ansible for a queued deployment. Callers must hold the lock."""
        check = deployment.check_mode
        deployment = self.repository.transition(
            deployment.id,
            DeploymentStatus.QUEUED,
            DeploymentStatus.RUNNING,
            started_at=datetime.now(UTC).isoformat(),
        )
        try:
            snapshot = self.repository.deployment_snapshot(deployment.id)
            self._write_desired_state(snapshot)
            result = self.runner.run(check=check, host_limit=deployment.host_limit)
            target = (
                DeploymentStatus.TIMED_OUT
                if result.timed_out
                else DeploymentStatus.SUCCEEDED
                if result.return_code == 0
                else DeploymentStatus.FAILED
            )
            deployment = self.repository.transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                target,
                finished_at=datetime.now(UTC).isoformat(),
                return_code=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except Exception as exc:
            deployment = self.repository.transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                DeploymentStatus.FAILED,
                finished_at=datetime.now(UTC).isoformat(),
                return_code=1,
                stdout="",
                stderr=f"deployment runner error: {type(exc).__name__}: {exc}",
            )
            self.repository.audit(
                operator,
                "deployment.failed",
                "deployment",
                deployment.id,
                {"error_type": type(exc).__name__},
            )
            if isinstance(exc, ExecutionError):
                raise
            return deployment
        self.repository.audit(
            operator,
            f"deployment.{deployment.status}",
            "deployment",
            deployment.id,
            {"return_code": deployment.return_code},
        )
        if (
            deployment.rollback_of
            and deployment.status is DeploymentStatus.SUCCEEDED
            and not check
        ):
            # Restore the zones the snapshot carried and re-derive from them,
            # so records and sites cannot end up disagreeing about what is
            # served.
            domains, records = self.repository.decode_snapshot_zones(snapshot)
            self.repository.replace_all_records(domains, records)
            self._sync_sites()
            self.repository.audit(
                operator,
                "rollback.applied",
                "deployment",
                deployment.id,
                {"target": deployment.rollback_of},
            )
        return deployment

    def _write_desired_state(self, snapshot: str) -> None:
        sites = self.repository.decode_snapshot(snapshot)
        documents: list[dict[str, object]] = []
        for site in sites:
            document = site.to_ansible()
            if site.certificate_mode in {
                CertificateMode.UPLOADED,
                CertificateMode.REQUESTED,
            }:
                certificate, private_key = self.certificate_store.sources(site.name)
                document["certificate_source_path"] = str(certificate)
                document["certificate_key_source_path"] = str(private_key)
            documents.append(document)
        atomic_write_yaml(
            self.settings.generated_vars_path,
            {
                "blitzecdn_desired_state_version": DESIRED_STATE_VERSION,
                "blitzecdn_nginx_sites": documents,
            },
        )
