from __future__ import annotations

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
    SitePatch,
    managed_certificate_paths,
)
from blitzecdn.exceptions import ConflictError, ExecutionError
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

    def create_site(self, site: CdnSite, operator: str) -> CdnSite:
        created = self.repository.create_site(site)
        self.repository.audit(operator, "site.created", "site", site.name)
        return created

    def update_site(self, name: str, patch: SitePatch, operator: str) -> CdnSite:
        updated = patch.apply(self.repository.get_site(name))
        saved = self.repository.replace_site(updated)
        self.repository.audit(
            operator,
            "site.updated",
            "site",
            name,
            {"fields": sorted(patch.model_fields_set)},
        )
        return saved

    def delete_site(self, name: str, operator: str) -> None:
        self.repository.delete_site(name)
        self.repository.audit(operator, "site.deleted", "site", name)

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

    def _activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite:
        certificate_path, certificate_key_path = managed_certificate_paths(site.name)
        updated = CdnSite.model_validate(
            {
                **site.model_dump(),
                "certificate_mode": mode,
                "certificate_path": certificate_path,
                "certificate_key_path": certificate_key_path,
            }
        )
        return self.repository.replace_site(updated)

    def validate(self) -> list[str]:
        errors = self.settings.validate_runtime()
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
            self.repository.replace_all_sites(self.repository.decode_snapshot(snapshot))
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
