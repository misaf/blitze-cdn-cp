from __future__ import annotations

import ipaddress
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from blitzecdn.config import Settings
from blitzecdn.domain.models import (
    DESIRED_STATE_VERSION,
    CdnSite,
    CertificateInfo,
    CertificateMode,
    CertificateSource,
    Deployment,
    DeploymentStatus,
    DnsRecord,
    Domain,
    RecordPatch,
    RecordType,
    managed_certificate_paths,
)
from blitzecdn.exceptions import ConflictError, ExecutionError, NotFoundError
from blitzecdn.infrastructure.ansible import AnsibleRunner
from blitzecdn.infrastructure.certificates import (
    CertbotIssuer,
    CertificateStore,
    Issuer,
)
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.filesystem import atomic_write_yaml

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
    ) -> None:
        self.settings = settings
        self.repository = repository or Repository(settings.database_path)
        self.runner = runner or AnsibleRunner(settings)
        self.certificate_store = certificate_store or CertificateStore(settings)
        self.issuer = issuer or CertbotIssuer(settings)

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

    def import_sites(
        self, domain_names: list[str], operator: str, *, force: bool = False
    ) -> dict[str, list[str]]:
        """Convert pre-1.1.0 sites into the proxied records that reproduce them.

        Sites created before records existed sit in a table that is now derived
        and rewritten whenever a record changes. Left alone they survive an
        upgrade and then vanish the first time anything touches a record. This
        is the upgrade path.

        Every zone must be imported in one call. The first import rewrites the
        derived table, which destroys the very sites a later call would have
        read — so importing one zone at a time would silently lose the rest.
        Rather than let that happen, a site not covered by ``domain_names`` is
        refused up front and nothing is written, unless ``force`` says the
        operator means to drop it.

        A site whose ``origin_host`` is a hostname cannot become an A record and
        is reported, not converted.
        """
        legacy = self.repository.list_sites()
        # Validates each zone and normalises case and trailing dots, so a
        # hostname comparison below cannot miss on formatting alone.
        zones = [Domain(name=name).name for name in domain_names]

        def zone_for(server_name: str) -> str | None:
            for zone in zones:
                if server_name == zone or server_name.endswith(f".{zone}"):
                    return zone
            return None

        uncovered = sorted(
            {
                server_name
                for site in legacy
                for server_name in site.server_names
                if zone_for(server_name) is None
            }
        )
        if uncovered and not force:
            raise ConflictError(
                "These hostnames belong to zones you did not list, and importing "
                "without them would stop them being served: "
                f"{', '.join(uncovered)}. Pass every zone in one command, or "
                "use --force to drop them deliberately."
            )

        for zone in zones:
            try:
                self.repository.get_domain(zone)
            except NotFoundError:
                self.create_domain(Domain(name=zone), operator)

        imported: list[str] = []
        skipped: list[str] = []

        for site in legacy:
            for server_name in site.server_names:
                matched = zone_for(server_name)
                if matched is None:
                    continue
                label = (
                    "@" if server_name == matched else server_name[: -len(matched) - 1]
                )
                try:
                    address = ipaddress.ip_address(site.origin_host)
                except ValueError:
                    skipped.append(
                        f"{server_name}: origin {site.origin_host!r} is a hostname, "
                        "and a record needs an IP address. Add the record by hand "
                        "with the address it resolves to."
                    )
                    continue
                record = DnsRecord(
                    domain=matched,
                    name=label,
                    type=RecordType.A if address.version == 4 else RecordType.AAAA,
                    value=site.origin_host,
                    proxied=True,
                    origin_port=site.origin_port,
                    origin_scheme=site.origin_scheme,
                    origin_request_host=site.origin_request_host,
                    origin_sni=site.origin_sni,
                    enabled=site.enabled,
                    cache_enabled=site.cache_enabled,
                    cache_valid_success=site.cache_valid_success,
                    cache_valid_not_found=site.cache_valid_not_found,
                )
                # Certificates live in a directory keyed by site name. Carry the
                # mode across only when the name the record derives matches the
                # one the certificate was filed under, otherwise the deploy
                # would point at a path holding nothing.
                if (
                    site.certificate_mode is not CertificateMode.DISABLED
                    and record.site_name == site.name
                ):
                    record = DnsRecord.model_validate(
                        {
                            **record.model_dump(),
                            "certificate_mode": site.certificate_mode,
                            "certificate_path": site.certificate_path,
                            "certificate_key_path": site.certificate_key_path,
                        }
                    )
                elif site.certificate_mode is not CertificateMode.DISABLED:
                    skipped.append(
                        f"{server_name}: certificate was stored under "
                        f"{site.name!r} but the record derives "
                        f"{record.site_name!r}. TLS is off on the imported "
                        "record; re-upload or re-request the certificate."
                    )
                try:
                    self.create_record(record, operator)
                except (ConflictError, NotFoundError) as exc:
                    skipped.append(f"{server_name}: {exc}")
                    continue
                imported.append(record.fqdn)

        self._sync_sites()
        served_after = {
            server_name
            for site in self._derive_sites()
            for server_name in site.server_names
        }
        dropped = sorted(
            {server_name for site in legacy for server_name in site.server_names}
            - served_after
        )

        self.repository.audit(
            operator,
            "sites.imported",
            "domain",
            ",".join(zones),
            {"imported": imported, "skipped": skipped, "dropped": dropped},
        )
        return {"imported": imported, "skipped": skipped, "dropped": dropped}

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

    def deploy(self, operator: str, *, check: bool = False) -> Deployment:
        """Converge every edge, returning once the run has finished."""
        with self.runner.lock():
            return self._converge(self._queue(operator, check=check), operator)

    def submit_deployment(self, operator: str, *, check: bool = False) -> Deployment:
        """Queue a convergence on a worker thread and return the queued record.

        A full run can take as long as ``deployment_timeout_seconds``, far
        longer than any HTTP client will wait, so callers poll
        ``GET /v1/deployments/{id}`` for the outcome.
        """
        return self._submit(lambda: self._queue(operator, check=check), operator)

    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Converge a prior snapshot and adopt it as canonical desired state."""
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
    ) -> Deployment:
        """Record a QUEUED deployment. Callers must hold the deployment lock."""
        deployment = self.repository.create_deployment(
            operator, check_mode=check, rollback_of=rollback_of, snapshot=snapshot
        )
        self.repository.audit(
            operator,
            "deployment.queued",
            "deployment",
            deployment.id,
            {"check_mode": check, "rollback_of": rollback_of},
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
            result = self.runner.run(check=check)
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
            # Restore the zones a snapshot carried and re-derive from them, so
            # records and sites cannot end up disagreeing about what is served.
            # A snapshot written before zones existed has none, and can only
            # restore the sites it recorded — say so rather than pretend the
            # rollback was complete.
            zones = self.repository.decode_snapshot_zones(snapshot)
            legacy = zones is None
            if zones is not None:
                domains, records = zones
                self.repository.replace_all_records(domains, records)
                self._sync_sites()
            else:
                self.repository.replace_all_sites(
                    self.repository.decode_snapshot(snapshot)
                )
            self.repository.audit(
                operator,
                "rollback.applied",
                "deployment",
                deployment.id,
                {"target": deployment.rollback_of, "pre_records_snapshot": legacy},
            )
            if legacy:
                _LOGGER.warning(
                    "deployment %s rolled back to a snapshot written before DNS "
                    "records existed; sites were restored but domains and "
                    "records were left untouched",
                    deployment.rollback_of,
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
