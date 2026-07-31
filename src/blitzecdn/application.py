from __future__ import annotations

from datetime import UTC, datetime

from blitzecdn.config import Settings
from blitzecdn.domain.models import CdnSite, Deployment, DeploymentStatus, SitePatch
from blitzecdn.exceptions import ConflictError, ExecutionError
from blitzecdn.infrastructure.ansible import AnsibleRunner
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.filesystem import atomic_write_yaml


class ControlPlane:
    """Coordinate persistence and Ansible at the application boundary."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository | None = None,
        runner: AnsibleRunner | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or Repository(settings.database_path)
        self.runner = runner or AnsibleRunner(settings)

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

    def deploy(
        self,
        operator: str,
        *,
        check: bool = False,
        snapshot: str | None = None,
        rollback_of: str | None = None,
    ) -> Deployment:
        with self.runner.lock():
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
            deployment = self.repository.transition(
                deployment.id,
                DeploymentStatus.QUEUED,
                DeploymentStatus.RUNNING,
                started_at=datetime.now(UTC).isoformat(),
            )
            try:
                desired_snapshot = self.repository.deployment_snapshot(deployment.id)
                self._write_desired_state(desired_snapshot)
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
            return deployment

    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
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
        snapshot = self.repository.deployment_snapshot(target.id)
        result = self.deploy(
            operator, check=check, snapshot=snapshot, rollback_of=target.id
        )
        if result.status is DeploymentStatus.SUCCEEDED and not check:
            self.repository.replace_all_sites(self.repository.decode_snapshot(snapshot))
            self.repository.audit(
                operator,
                "rollback.applied",
                "deployment",
                result.id,
                {"target": target.id},
            )
        return result

    def _write_desired_state(self, snapshot: str) -> None:
        sites = self.repository.decode_snapshot(snapshot)
        atomic_write_yaml(
            self.settings.generated_vars_path,
            {"blitzecdn_nginx_sites": [site.to_ansible() for site in sites]},
        )
