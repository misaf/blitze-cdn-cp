"""Convergence, history, rollback, and drift.

Everything here is about turning a stored snapshot into a run of Ansible and
recording what happened. Nothing here decides *what* should be deployed; that
is the zone editor's job, and this service reads its output through
``DnsService.sync_sites`` only when a rollback rewrites canonical state.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from blitzecdn.application.dns import DnsService
from blitzecdn.config import Settings
from blitzecdn.domain.deployments import Deployment, DeploymentStatus, DriftReport
from blitzecdn.domain.runs import AnsibleRun, RunStatus
from blitzecdn.domain.sites import DESIRED_STATE_VERSION, CertificateMode
from blitzecdn.domain.snapshots import decode_snapshot, decode_snapshot_zones
from blitzecdn.domain.validation import validate_edge_limit
from blitzecdn.exceptions import ConflictError, ExecutionError
from blitzecdn.ports import (
    AuditLog,
    CertificateStore,
    DeploymentRunner,
    DeploymentStore,
    LogReader,
    ZoneStore,
)

_LOGGER = logging.getLogger(__name__)

#: How far back to look for the deployment that last carried a site. Only the
#: newest successful run is used; the window exists so a burst of failed
#: attempts cannot hide it.
_DEPLOYMENT_LOOKBACK = 50


class DeploymentService:
    """Runs Ansible against a recorded snapshot and owns the deployment lock."""

    def __init__(
        self,
        settings: Settings,
        deployments: DeploymentStore,
        zones: ZoneStore,
        audit_log: AuditLog,
        runner: DeploymentRunner,
        certificate_store: CertificateStore,
        dns: DnsService,
        write_yaml: Callable[[Path, dict[str, object]], None],
        read_log: LogReader,
    ) -> None:
        self.settings = settings
        self.deployments = deployments
        self.zones = zones
        self.audit_log = audit_log
        self.runner = runner
        self.certificate_store = certificate_store
        self.dns = dns
        #: Injected rather than imported so this layer never reaches for the
        #: filesystem adapter directly. The composition root supplies the
        #: atomic writer; a test can supply anything with the same shape.
        self.write_yaml = write_yaml
        #: Quotes a run log into a message for an operator. Never branched on:
        #: `validate` is the one caller, and only because `--syntax-check` runs
        #: no play and so leaves the callback nothing to report.
        self.read_log = read_log

    def initialize(self) -> int:
        return self.deployments.abandon_running()

    # -- Validation ----------------------------------------------------

    def validate(self) -> list[str]:
        """Answer whether desired state is coherent and the play parses.

        Renders to a scratch file rather than to ``generated_vars_path``.
        Validation is a question, not a publication, and it takes no lock — so
        writing to the real file would let it land between the moment a deploy
        in another process wrote its snapshot there and the moment Ansible read
        it. A rollback is where that hurts most: the fleet would converge to
        current state while the rollback still rewrote canonical records to the
        old snapshot's zones, leaving the control plane and the edges disagreeing
        in exactly the way rollback exists to end.
        """
        errors = self.settings.validate_runtime()
        errors.extend(self.dns.validation_errors())
        if not errors:
            with self._scratch_desired_state(self.deployments.snapshot()) as variables:
                run = self.runner.validate(variables)
            if not run.succeeded:
                # The one place a log is read back. `--syntax-check` executes no
                # play, so there is no structured result to explain a refusal —
                # the return code decides, and Ansible's own message is quoted
                # so the operator does not have to go and find it.
                errors.append(
                    self.read_log(run.log_path, limit=self.settings.output_limit_bytes)
                    or run.summary()
                )
        return errors

    @contextmanager
    def _scratch_desired_state(self, snapshot: str) -> Iterator[Path]:
        """Render a snapshot somewhere only this call can see, then drop it."""
        directory = self.settings.run_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"validate-{uuid4().hex}.yml"
        try:
            self.write_desired_state(snapshot, path)
            yield path
        finally:
            path.unlink(missing_ok=True)

    # -- Deploying -----------------------------------------------------

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
            return self.converge(
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
            return self.converge(
                self._queue_rollback(operator, deployment_id, check=check), operator
            )

    def submit_rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        """Queue a rollback on a worker thread and return the queued record."""
        return self._submit(
            lambda: self._queue_rollback(operator, deployment_id, check=check), operator
        )

    # -- Drift ---------------------------------------------------------

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
        self.audit_log.audit(
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
        the answer a scheduled check produced without re-running it. The stored
        result is the structured one, so this reads the same object a live run
        produced rather than re-deriving anything.
        """
        deployment = self.deployments.get_deployment(deployment_id)
        if not deployment.check_mode:
            raise ConflictError(
                f"deployment {deployment_id} applied changes rather than "
                "previewing them, so its result describes what it did, not "
                "what had drifted. Run 'blitzecdn drift' instead."
            )
        return DriftReport.of(deployment)

    # -- History -------------------------------------------------------

    def site_is_deployed(self, site_name: str) -> bool:
        """Whether the most recent real deployment carried this site.

        Check-mode runs are skipped: they proved the play parses, not that any
        edge is serving the vhost. A run narrowed by ``host_limit`` counts,
        because it did install the site somewhere — which makes this check
        necessary rather than sufficient, the same caveat rollback carries about
        canaries. Only the newest successful run is consulted; an older one
        listing the site says nothing about whether it is still deployed.
        """
        for deployment in self.deployments.list_deployments(limit=_DEPLOYMENT_LOOKBACK):
            if deployment.status is not DeploymentStatus.SUCCEEDED:
                continue
            if deployment.check_mode:
                continue
            snapshot = self.deployments.deployment_snapshot(deployment.id)
            return any(site.name == site_name for site in decode_snapshot(snapshot))
        return False

    # -- Internals -----------------------------------------------------

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
        deployment = self.deployments.create_deployment(
            operator,
            check_mode=check,
            rollback_of=rollback_of,
            snapshot=snapshot,
            host_limit=limit,
        )
        self.audit_log.audit(
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
        return self._queue(
            operator,
            check=check,
            snapshot=self.deployments.deployment_snapshot(target.id),
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
                self.converge(deployment, operator)
            except Exception:
                _LOGGER.exception("deployment %s failed", deployment.id)
            finally:
                lock.__exit__(None, None, None)

        # The lock is released by whoever ends up owning the deployment, and
        # until the thread is actually running that is still this call. A
        # thread that cannot be started would otherwise leave the lock held by
        # a worker that does not exist, and every later deploy, rollback,
        # upload and issuance would fail with DeploymentBusyError until the
        # process was restarted — a permanent outage from a transient failure.
        try:
            threading.Thread(
                target=worker, name=f"blitzecdn-deploy-{deployment.id}", daemon=True
            ).start()
        except BaseException as exc:
            try:
                self.deployments.transition(
                    deployment.id,
                    DeploymentStatus.QUEUED,
                    DeploymentStatus.FAILED,
                    finished_at=datetime.now(UTC).isoformat(),
                    result=self._aborted_run(exc, interrupted=False),
                )
                self.audit_log.audit(
                    operator,
                    "deployment.failed",
                    "deployment",
                    deployment.id,
                    {"error_type": type(exc).__name__},
                )
            finally:
                lock.__exit__(None, None, None)
            raise
        return deployment

    def converge(self, deployment: Deployment, operator: str) -> Deployment:
        """Run Ansible for a queued deployment. Callers must hold the lock."""
        check = deployment.check_mode
        deployment = self.deployments.transition(
            deployment.id,
            DeploymentStatus.QUEUED,
            DeploymentStatus.RUNNING,
            started_at=datetime.now(UTC).isoformat(),
        )
        try:
            snapshot = self.deployments.deployment_snapshot(deployment.id)
            self.write_desired_state(snapshot, self.settings.generated_vars_path)
            run = self.runner.run(check=check, host_limit=deployment.host_limit)
            deployment = self.deployments.transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                DeploymentStatus.of(run),
                finished_at=datetime.now(UTC).isoformat(),
                result=run,
            )
        except BaseException as exc:
            interrupted = not isinstance(exc, Exception)
            deployment = self.deployments.transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                (
                    DeploymentStatus.ABANDONED
                    if interrupted
                    else DeploymentStatus.FAILED
                ),
                finished_at=datetime.now(UTC).isoformat(),
                # The runner never produced a result, so one is synthesised for
                # the record: a deployment that ended without a run still has to
                # say why, and every reader now expects to find that in `result`.
                result=self._aborted_run(exc, interrupted=interrupted),
            )
            self.audit_log.audit(
                operator,
                "deployment.abandoned" if interrupted else "deployment.failed",
                "deployment",
                deployment.id,
                {"error_type": type(exc).__name__},
            )
            if interrupted:
                raise
            if isinstance(exc, ExecutionError):
                raise
            return deployment
        self.audit_log.audit(
            operator,
            f"deployment.{deployment.status}",
            "deployment",
            deployment.id,
            {
                "return_code": run.return_code,
                "changed": [host.host for host in run.changed_hosts],
                "failed": [host.host for host in run.failed_hosts],
            },
        )
        if (
            deployment.rollback_of
            and deployment.status is DeploymentStatus.SUCCEEDED
            and not check
        ):
            # Restore the zones the snapshot carried and re-derive from them,
            # so records and sites cannot end up disagreeing about what is
            # served.
            domains, records = decode_snapshot_zones(snapshot)
            self.zones.replace_all_records(domains, records)
            self.dns.sync_sites()
            self.audit_log.audit(
                operator,
                "rollback.applied",
                "deployment",
                deployment.id,
                {"target": deployment.rollback_of},
            )
        return deployment

    @staticmethod
    def _aborted_run(exc: BaseException, *, interrupted: bool) -> AnsibleRun:
        """A result for a deployment that never got a run of its own.

        The runner raised before Ansible reported anything — a misconfiguration
        it refused to start with, or the process being interrupted. There are no
        hosts to describe, so this records why in the same shape every other
        outcome uses rather than leaving ``result`` empty and making every
        reader handle a second way of saying "it failed".
        """
        now = datetime.now(UTC)
        return AnsibleRun(
            id=uuid4().hex,
            playbook="",
            status=RunStatus.UNSTARTED,
            return_code=None,
            started_at=now,
            finished_at=now,
            error=(
                f"deployment interrupted: {type(exc).__name__}"
                if interrupted
                else f"deployment runner error: {type(exc).__name__}: {exc}"
            ),
        )

    def write_desired_state(self, snapshot: str, path: Path) -> None:
        """Render a snapshot as the document Ansible reads with ``--extra-vars``.

        The destination is a parameter because two callers want different ones:
        a deploy publishes to ``generated_vars_path`` under the lock, while
        ``validate`` renders to a scratch path it then throws away.
        """
        sites = decode_snapshot(snapshot)
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
        self.write_yaml(
            path,
            {
                "blitzecdn_desired_state_version": DESIRED_STATE_VERSION,
                "blitzecdn_nginx_allow_empty_sites": self.settings.allow_empty_sites,
                "blitzecdn_nginx_sites": documents,
            },
        )
