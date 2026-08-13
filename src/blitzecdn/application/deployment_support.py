"""Focused collaborators used by deployment lifecycle orchestration."""

from __future__ import annotations

from pathlib import Path

from blitzecdn.config import Settings
from blitzecdn.domain.deployments import Deployment, DeploymentStatus, DriftReport
from blitzecdn.domain.sites import CertificateMode
from blitzecdn.domain.snapshots import decode_snapshot
from blitzecdn.exceptions import ConflictError
from blitzecdn.ports import CertificateStore, DeploymentStore, YamlWriter


class DesiredStateRenderer:
    """Transforms immutable canonical snapshots into Ansible input."""

    def __init__(
        self,
        settings: Settings,
        certificates: CertificateStore,
        write_yaml: YamlWriter,
    ) -> None:
        self.settings = settings
        self.certificates = certificates
        self.write_yaml = write_yaml

    def render(self, snapshot: str, path: Path) -> None:
        documents: list[dict[str, object]] = []
        for site in decode_snapshot(snapshot):
            document = site.to_ansible()
            if site.certificate_mode in {
                CertificateMode.UPLOADED,
                CertificateMode.REQUESTED,
            }:
                certificate, private_key = self.certificates.sources(site.name)
                document["certificate_source_path"] = str(certificate)
                document["certificate_key_source_path"] = str(private_key)
            documents.append(document)
        self.write_yaml(
            path,
            {
                "blitzecdn_nginx_allow_empty_sites": self.settings.allow_empty_sites,
                "blitzecdn_nginx_sites": documents,
            },
        )


class RollbackPlanner:
    """Selects and validates rollback targets without executing them."""

    def __init__(self, deployments: DeploymentStore) -> None:
        self.deployments = deployments

    def target(self, deployment_id: str | None) -> Deployment:
        target = (
            self.deployments.get_deployment(deployment_id)
            if deployment_id
            else self.deployments.successful_rollback_target(
                self.deployments.snapshot()
            )
        )
        if target.check_mode or target.status is not DeploymentStatus.SUCCEEDED:
            raise ConflictError(
                "rollback target must be a successful applied deployment"
            )
        return target


class DriftInterpreter:
    """Reads a stored check-mode deployment as a drift report."""

    @staticmethod
    def report(deployment: Deployment) -> DriftReport:
        if not deployment.check_mode:
            raise ConflictError(
                f"deployment {deployment.id} applied changes rather than "
                "previewing them, so its result describes what it did, not "
                "what had drifted. Run 'blitzecdn drift' instead."
            )
        return DriftReport.of(deployment)
