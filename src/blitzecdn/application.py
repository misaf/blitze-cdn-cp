from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from blitzecdn.config import Settings
from blitzecdn.domain.models import (
    CERTIFICATE_RENEWAL_DAYS,
    DESIRED_STATE_VERSION,
    CacheStatsReport,
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
    EdgeStats,
    HostDrift,
    OriginCheck,
    PreflightReport,
    PurgeEntry,
    PurgeResult,
    RecordPatch,
    RecordType,
    SiteCacheStats,
    managed_certificate_paths,
    validate_edge_limit,
)
from blitzecdn.exceptions import (
    BlitzeError,
    ConfigurationError,
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
from blitzecdn.infrastructure.inventory import Inventory
from blitzecdn.infrastructure.origins import OriginProbe
from blitzecdn.infrastructure.preflight import CertificatePreflight

_LOGGER = logging.getLogger(__name__)

#: How far back to look for the deployment that last carried a site. Only the
#: newest successful run is used; the window exists so a burst of failed
#: attempts cannot hide it.
_DEPLOYMENT_LOOKBACK = 50


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
        preflight: CertificatePreflight | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or Repository(settings.database_path)
        self.runner = runner or AnsibleRunner(settings)
        self.certificate_store = certificate_store or CertificateStore(settings)
        self.issuer = issuer or CertbotIssuer(settings)
        self.origin_probe = origin_probe or OriginProbe(settings)
        self.preflight = preflight or CertificatePreflight(
            settings, origin_probe=self.origin_probe
        )

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

    def certificate_preflight(self, name: str) -> PreflightReport:
        """Report whether HTTP-01 could validate this site right now.

        Public so an operator can ask before spending a rate-limited request,
        and so the answer to "why did issuance fail" does not require reading
        certbot's output.
        """
        site = self.repository.get_site(name)
        record = self._record_for_site(site.name)
        return self.preflight.check(
            site,
            deployed=self._site_is_deployed(site.name),
            record_ttl=record.ttl,
        )

    def _site_is_deployed(self, site_name: str) -> bool:
        """Whether the most recent real deployment carried this site.

        Check-mode runs are skipped: they proved the play parses, not that any
        edge is serving the vhost. A run narrowed by ``host_limit`` counts,
        because it did install the site somewhere — which makes this check
        necessary rather than sufficient, the same caveat rollback carries about
        canaries. Only the newest successful run is consulted; an older one
        listing the site says nothing about whether it is still deployed.
        """
        for deployment in self.repository.list_deployments(limit=_DEPLOYMENT_LOOKBACK):
            if deployment.status is not DeploymentStatus.SUCCEEDED:
                continue
            if deployment.check_mode:
                continue
            snapshot = self.repository.deployment_snapshot(deployment.id)
            return any(
                site.name == site_name for site in Repository.decode_snapshot(snapshot)
            )
        return False

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
            deployed=self._site_is_deployed(site.name),
            record_ttl=self._record_for_site(site.name).ttl,
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
        self.repository.audit(
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
        _LOGGER.warning(
            "certificate preflight overridden for %s: %s",
            site.name,
            report.summary(),
        )
        return report

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
            site = self.repository.get_site(name)
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
        self._activate_managed_certificate(site, CertificateMode.REQUESTED)
        self.repository.audit(
            operator,
            "certificate.requested",
            "site",
            site.name,
            {"domains": list(info.domains), "not_after": info.not_after.isoformat()},
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
        for candidate in self.repository.list_sites():
            if candidate.certificate_mode is not CertificateMode.DISABLED:
                continue
            report = self.certificate_preflight(candidate.name)
            if not report.ok:
                skipped[candidate.name] = report.summary()
                continue
            try:
                with self.runner.lock():
                    site = self.repository.get_site(candidate.name)
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

        deployment = self.deploy(operator) if issued else None
        return {
            "issued": issued,
            "skipped": skipped,
            "failed": failed,
            "deployment": deployment,
        }

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
        sites: Sequence[str] | None = None,
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
        """
        renewed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        now = datetime.now(UTC)
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
                # Without this the audit trail cannot tell a full sweep that
                # renewed one certificate from a narrowed run that only looked
                # at one, which is the question asked after a missed expiry.
                "sites": None if sites is None else sorted(sites),
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

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def purge_cache(
        self,
        operator: str,
        *,
        entries: Sequence[PurgeEntry] = (),
        purge_all: bool = False,
        host_limit: str | None = None,
    ) -> PurgeResult:
        """Remove cached responses from the edges.

        Which hostnames may be purged is decided here rather than on the edge:
        a hostname no site serves is refused, because a purge that quietly
        matches nothing is indistinguishable from one that worked, and the
        operator learns nothing until the stale object is still being served.

        Purging is not deploying. Nothing about desired state changes, no
        deployment record is written, and the deployment lock is not taken —
        the moment a purge is most needed is the moment a deploy is most likely
        to already be running.
        """
        if purge_all and entries:
            raise ConflictError(
                "purge everything and purge named entries are different "
                "operations; ask for one or the other"
            )
        if not purge_all and not entries:
            raise ConflictError(
                "nothing to purge: give at least one entry or purge all"
            )
        if entries:
            self._reject_unserved_hosts(entries)
        result = self.runner.run_cache_purge(
            entries=[entry.to_ansible() for entry in entries],
            purge_all=purge_all,
            host_limit=host_limit,
        )
        report = PurgeResult(
            purged_at=datetime.now(UTC),
            entries=tuple(entries),
            purge_all=purge_all,
            host_limit=host_limit,
            hosts=parse_play_recap(result.stdout),
        )
        self.repository.audit(
            operator,
            "cache.purged",
            "site",
            None,
            {
                "purge_all": purge_all,
                "entries": [entry.to_ansible() for entry in entries],
                "host_limit": host_limit,
                "complete": report.complete,
                "failed": [host.host for host in report.failed],
            },
        )
        if not report.hosts:
            raise ExecutionError(
                "no edge reported a purge result. "
                + (result.stderr.strip() or "Check the inventory and the run above.")
            )
        return report

    def decommission_edge(
        self, name: str, operator: str, *, force: bool = False
    ) -> tuple[HostDrift, ...]:
        """Strip an edge of BlitzeCDN state, then take it out of inventory.

        The order is the whole point. Removing the inventory entry first would
        leave a host that no playbook can address again, still serving the
        virtual hosts it last converged and still holding the private keys for
        them. So the teardown runs while the host is still reachable, and the
        entry is deleted only once it reports clean.

        ``force`` drops the entry after a failed or unreachable teardown. It is
        for a host that no longer exists — a destroyed instance cannot be
        cleaned and would otherwise block its own removal forever. On a host
        that is merely down it is the wrong answer: the keys stay where they
        are and nothing will ever come back for them.
        """
        inventory = Inventory(self.settings.inventory_path)
        if name not in {edge["name"] for edge in inventory.list_edges()}:
            raise ConfigurationError(f"edge does not exist: {name}")

        hosts: tuple[HostDrift, ...] = ()
        failure: str | None = None
        try:
            result = self.runner.run_decommission(host_limit=name)
        except ExecutionError as error:
            failure = str(error)
        else:
            hosts = parse_play_recap(result.stdout)
            # Deliberately not HostDrift.in_sync: a teardown that removed
            # anything reports changed>0, which is what success looks like
            # here. Only failed and unreachable mean files were left behind.
            if result.return_code != 0 or any(
                host.failed or host.unreachable for host in hosts
            ):
                failure = (
                    result.stderr.strip()
                    or "the teardown play did not report a clean host"
                )
            elif not hosts:
                failure = "no edge reported a teardown result"

        self.repository.audit(
            operator,
            "edge.decommissioned" if failure is None else "edge.decommission_failed",
            "edge",
            name,
            {
                "forced": force,
                "hosts": [host.host for host in hosts],
                **({} if failure is None else {"error": failure}),
            },
        )
        if failure is not None and not force:
            raise ExecutionError(
                f"teardown of {name} failed and the inventory entry was kept: "
                f"{failure} Retry once the host is reachable, or pass --force "
                "if the host no longer exists — forcing leaves its "
                "configuration and private keys in place."
            )
        inventory.remove_edge(name)
        return hosts

    def _reject_unserved_hosts(self, entries: Sequence[PurgeEntry]) -> None:
        """Refuse a hostname no enabled site answers to.

        Wildcard server names match their subdomains, the same way nginx
        matches them, so purging ``a.cdn.example.com`` under a ``*.example.com``
        site is allowed.
        """
        served: set[str] = set()
        wildcards: set[str] = set()
        for site in self.repository.list_sites():
            if not site.enabled:
                continue
            for name in site.server_names:
                if name.startswith("*."):
                    wildcards.add(name[2:])
                else:
                    served.add(name)
        unknown = sorted(
            {
                entry.host
                for entry in entries
                if entry.host not in served
                and not any(entry.host.endswith(f".{suffix}") for suffix in wildcards)
            }
        )
        if unknown:
            raise NotFoundError(
                "no enabled site serves: "
                + ", ".join(unknown)
                + ". A purge for a hostname nothing serves would report success "
                "having removed nothing."
            )

    def cache_stats(
        self, operator: str, *, host_limit: str | None = None
    ) -> CacheStatsReport:
        """Collect cache effectiveness from the edges.

        Read-only and unlocked. The edges write one JSON document each into a
        directory under the state dir, which is emptied first so a silent edge
        cannot be answered with its own stale report from a previous run.
        """
        output_dir = self.settings.state_dir / "stats"
        _reset_directory(output_dir)
        result = self.runner.run_stats(output_dir=output_dir, host_limit=host_limit)
        edges = _read_edge_reports(output_dir, parse_play_recap(result.stdout))
        report = CacheStatsReport(
            collected_at=datetime.now(UTC), host_limit=host_limit, edges=edges
        )
        self.repository.audit(
            operator,
            "cache.stats_collected",
            "deployment",
            None,
            {
                "host_limit": host_limit,
                "reporting": [edge.host for edge in report.reporting],
                "silent": [edge.host for edge in report.silent],
                "hit_ratio": report.hit_ratio,
            },
        )
        if not report.edges:
            raise ExecutionError(
                "no edge reported statistics. "
                + (result.stderr.strip() or "Check the inventory and the run above.")
            )
        return report

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


def _reset_directory(path: Path) -> None:
    """Empty a directory, creating it if absent.

    Emptied rather than merged into: an edge that fails to report this run must
    appear as silent, not be answered with the document it left behind last
    time. A stale number read as current is the failure mode that matters here.
    """
    if path.exists():
        for child in path.iterdir():
            if child.is_file():
                child.unlink()
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _read_edge_reports(
    output_dir: Path, recap: Sequence[HostDrift]
) -> tuple[EdgeStats, ...]:
    """Turn the per-edge JSON documents into domain objects.

    Driven by the play recap rather than by whatever files happen to be on
    disk, so an edge that ran but produced nothing is reported as silent
    instead of vanishing from the fleet — the recap is the roster.
    """
    edges: list[EdgeStats] = []
    for host in recap:
        path = output_dir / f"{host.host}.json"
        if host.unreachable:
            edges.append(EdgeStats(host=host.host, error="unreachable"))
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            edges.append(EdgeStats(host=host.host, error=f"no usable report: {exc}"))
            continue
        edges.append(_edge_stats(host.host, document))
    return tuple(edges)


def _edge_stats(host: str, document: object) -> EdgeStats:
    """Read one edge's document defensively.

    The shape is ours, but it crossed a machine boundary and a partially
    written or truncated file must degrade to "this edge said nothing usable"
    rather than raise out of a fleet-wide report.
    """
    if not isinstance(document, dict):
        return EdgeStats(host=host, error="report was not an object")
    outcomes: dict[str, dict[str, int]] = {}
    for row in document.get("cache") or []:
        if not isinstance(row, dict):
            continue
        site = str(row.get("site", "")).strip()
        outcome = str(row.get("outcome", "")).strip().upper()
        try:
            requests = int(row.get("requests", 0))
        except (TypeError, ValueError):
            continue
        if not site or not outcome or requests < 0:
            continue
        outcomes.setdefault(site, {})[outcome] = (
            outcomes.setdefault(site, {}).get(outcome, 0) + requests
        )
    connections = {
        str(key): int(value)
        for key, value in (document.get("connections") or {}).items()
        if isinstance(value, (int, str)) and str(value).isdigit()
    }
    collected = document.get("collected_at")
    try:
        collected_at = datetime.fromisoformat(str(collected)) if collected else None
    except ValueError:
        collected_at = None
    return EdgeStats(
        host=host,
        collected_at=collected_at,
        nginx_reachable=bool(document.get("nginx_reachable")),
        connections=connections,
        sites=tuple(
            SiteCacheStats(site=name, outcomes=counts)
            for name, counts in sorted(outcomes.items())
        ),
    )
