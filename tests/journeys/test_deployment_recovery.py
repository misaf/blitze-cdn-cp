"""Recovery across durable jobs, deployment rows, workflow journals and target fences.

Each scenario runs two production service graphs over one disposable database:
worker A, which stops or stalls at a named persistence boundary, and worker B,
which takes the job over once A's lease has expired. An interrupt
(`SystemExit`) stands in for a kill, because the code under test records
nothing on one, exactly as after a SIGKILL; `test_process_recovery` kills a real
process. Ansible and the network are replaced by one model of the fleet that
both workers drive, so what the edges serve can be checked against what the
control plane recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.control_plane import (
    ControlPlane,
    ansible_run,
    build_application,
    host_run,
    install_worker,
    seed_edge,
    seed_site,
)
from support.stores import Stores

from blitzecdn.capabilities.deployments.domain import (
    DEPLOYMENT_WORKFLOW,
    PHASE_ORDER,
    ROLLBACK_WORKFLOW,
    DeploymentRequirementKind,
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.deployments.service.rollback import CANONICAL_CHANGED
from blitzecdn.capabilities.jobs.domain import JobStatus
from blitzecdn.capabilities.sites.domain import DnsRecord, RecordType
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.core.ansible.variables import read_variables
from blitzecdn.core.domain.runs import AnsibleRun, RunStatus

EDGES = ("edge-a", "edge-b")
ADDRESSES = {"edge-a": "192.0.2.10", "edge-b": "192.0.2.11"}


class Fleet:
    """The edges as they would answer from outside: which release each serves.

    Activation makes an edge serve the desired-state file's release, as the play
    does, and the serving probe answers from the same record, so verification
    and recovery observation see what activation actually left behind.
    """

    def __init__(self, desired_state: Path) -> None:
        self.desired_state = desired_state
        self.serving: dict[str, str] = {}
        #: Every probe, as ``(edge, release digest asked for)``.
        self.observed: list[tuple[str, str]] = []

    def activate(self, edges: Sequence[str]) -> None:
        digest = read_variables(self.desired_state)["blitzecdn_release_digest"]
        for edge in edges:
            self.serving[edge] = str(digest)

    def verify(
        self, *, edge: str, address: str, sites: Sequence[Any], release_digest: str
    ) -> tuple[Any, ...]:
        assert ADDRESSES[edge] == address
        self.observed.append((edge, release_digest))
        served = self.serving.get(edge) == release_digest
        return tuple(
            SimpleNamespace(
                edge=edge,
                hostname=site.server_names[0],
                served=served,
                detail="HTTP 200" if served else "serving another release",
            )
            for site in sites
            if site.server_names
        )


class FleetRunner:
    """One worker's Ansible: drives the shared fleet, calling ``pause`` first.

    ``pause`` sees each run as ``(tags, edges)`` before it touches the fleet, so a
    test can stop the worker at the boundary that run stands for.
    """

    def __init__(
        self,
        fleet: Fleet,
        pause: Callable[[tuple[str, ...], tuple[str, ...]], None] | None = None,
    ) -> None:
        self.fleet = fleet
        self.pause = pause
        #: Every run, as ``(phase, edges)``; validation is the tagless check run.
        self.runs: list[tuple[str, tuple[str, ...]]] = []

    def lock(self) -> nullcontext[None]:
        # Both workers share one fleet; the fence, not the lock, is under test.
        return nullcontext()

    def validate(self, variables: Path) -> AnsibleRun:
        return ansible_run(*(host_run(edge) for edge in EDGES))

    def run(
        self, *, check: bool, host_limit: str | None = None, tags: tuple[str, ...] = ()
    ) -> AnsibleRun:
        edges = tuple(host_limit.split(",")) if host_limit else EDGES
        if self.pause is not None:
            self.pause(tags, edges)
        self.runs.append((tags[0] if tags else "validate", edges))
        if tags == ("activate",) and not check:
            self.fleet.activate(edges)
        return ansible_run(*(host_run(edge) for edge in edges))

    def run_playbook(self, **_: Any) -> AnsibleRun:
        raise AssertionError("no named play belongs in a deployment")


class Worker:
    """One process's graph and job queue, on its own connection to the database."""

    def __init__(self, settings: Any, fleet: Fleet, clock: Callable[[], datetime]):
        self.runner = FleetRunner(fleet)
        self.repository = Stores(settings.database_path)
        self.control: ControlPlane = build_application(
            settings=settings,
            repository=self.repository,
            runner=self.runner,
            serving_probe=fleet,
        )
        install_worker(self.control)
        self.control.job_queue.clock = clock

    def work(self, *, lease_seconds: int = 60) -> Any:
        claim = self.control.job_queue.claim(lease_seconds=lease_seconds)
        assert claim is not None, "no job was ready to claim"
        self.control.job_queue.run(claim)
        return claim

    def die(self, *, lease_seconds: int = 1) -> Any:
        """Claim the next job and run it until the interrupt this worker is set for."""
        claim = self.control.job_queue.claim(lease_seconds=lease_seconds)
        assert claim is not None, "no job was ready to claim"
        with pytest.raises(SystemExit):
            self.control.job_queue.run(claim)
        return claim


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def expire(self, claim: Any) -> None:
        """Move past ``claim``'s lease, as a dead worker's lease runs out."""
        self.now = max(self.now, claim.job.leased_until) + timedelta(seconds=1)


@pytest.fixture
def cluster(settings):
    fleet = Fleet(settings.generated_vars_path)
    clock = Clock()
    a = Worker(settings, fleet, clock)
    b = Worker(settings, fleet, clock)
    for edge in EDGES:
        seed_edge(a.control, name=edge, host=ADDRESSES[edge])
    seed_site(a.control)
    try:
        yield SimpleNamespace(fleet=fleet, clock=clock, a=a, b=b)
    finally:
        a.control.close()
        b.control.close()
        a.repository.close()
        b.repository.close()


def interrupt() -> None:
    raise SystemExit("worker stopped at a checkpoint")


def stop_a_at(cluster: Any, boundary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make worker A stop at ``boundary`` on the canary, edge-a.

    ``planned``: the rollout was taken over and planned, no edge begun.
    ``staged``: edge-a staged, not activated.
    ``activated``: edge-a serving the new release, not committed.
    ``converged``: every edge done, the deployment row not yet finished.
    """
    service = cluster.a.control.deployments
    if boundary == "planned":
        monkeypatch.setattr(
            service.execution.rollout.targets, "begin", lambda *a, **k: interrupt()
        )
    elif boundary == "converged":
        store = service.persistence.deployments
        save = store.save

        def finishing(before, after):
            if after.finished_at is not None:
                interrupt()
            return save(before, after)

        monkeypatch.setattr(store, "save", finishing)
    else:
        tag = {"staged": "activate", "activated": "commit"}[boundary]

        def pause(tags, edges):
            if tags == (tag,) and "edge-a" in edges:
                interrupt()

        cluster.a.runner.pause = pause


def targets_of(control: ControlPlane, deployment_id: str) -> dict[str, Any]:
    return {
        target.edge: target for target in control.deployments.targets(deployment_id)
    }


def journal_for(repository: Stores, deployment_id: str) -> list[Any]:
    """The deployment's workflows, oldest first."""
    return sorted(
        (
            workflow
            for workflow in repository.workflows.list_workflows()
            if workflow.resource_id == deployment_id
        ),
        key=lambda workflow: workflow.created_at,
    )


def actions_for(repository: Stores, deployment_id: str) -> list[str]:
    return [
        event.action
        for event in reversed(repository.audit_log.list_audit_events(200))
        if event.resource_id == deployment_id
    ]


def assert_converged(control: ControlPlane, fleet: Fleet, deployment: Any) -> None:
    """Rows, fences and the edges agree that ``deployment``'s release is out."""
    release = control.releases.get(deployment.release_id)
    generation = control.repository.deployment_targets.generation(deployment.id)
    targets = targets_of(control, deployment.id)
    assert set(targets) == set(EDGES)
    for target in targets.values():
        assert target.status is TargetStatus.SUCCEEDED
        assert target.phase is TargetPhase.VERIFY
        assert target.fence == generation
    assert fleet.serving == dict.fromkeys(EDGES, release.digest)


# -- Interruption, then takeover ---------------------------------------


def _phases(phases: Sequence[str], edge: str) -> list[tuple[str, tuple[str, ...]]]:
    return [(phase, (edge,)) for phase in phases]


#: What B still has to run after A stopped at each boundary on the canary.
_RESUMED_RUNS = {
    "planned": [
        *_phases(("validate", "stage", "activate", "commit"), "edge-a"),
        *_phases(("validate", "stage", "activate", "commit"), "edge-b"),
    ],
    "staged": [
        *_phases(("activate", "commit"), "edge-a"),
        *_phases(("validate", "stage", "activate", "commit"), "edge-b"),
    ],
    "activated": [
        *_phases(("commit",), "edge-a"),
        *_phases(("validate", "stage", "activate", "commit"), "edge-b"),
    ],
    "converged": [],
}


@pytest.mark.parametrize("boundary", list(_RESUMED_RUNS))
def test_takeover_resumes_an_interrupted_rollout_under_a_new_fence(
    cluster, monkeypatch, boundary
):
    a, b = cluster.a, cluster.b
    queued = a.control.deployments.submit_deployment("alice")
    stop_a_at(cluster, boundary, monkeypatch)

    stranded = a.die()

    # A left everything in flight: nothing recorded an outcome for it.
    assert a.control.deployments.get_deployment(queued.id).status is (
        DeploymentStatus.RUNNING
    )
    job = a.control.job_queue.get(stranded.job.id)
    assert job.status is JobStatus.RUNNING and job.fence == stranded.fence
    [interrupted] = journal_for(a.repository, queued.id)
    assert interrupted.status is WorkflowStatus.RUNNING
    fence_a = a.repository.deployment_targets.generation(queued.id)

    cluster.clock.expire(stranded)
    taken = b.work()

    assert taken.job.id == stranded.job.id and taken.fence > stranded.fence
    job = b.control.job_queue.get(taken.job.id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempts == 2 and job.last_error is None
    deployment = b.control.deployments.get_deployment(queued.id)
    assert deployment.status is DeploymentStatus.SUCCEEDED
    assert b.repository.deployment_targets.generation(queued.id) > fence_a
    assert_converged(b.control, cluster.fleet, deployment)
    assert b.runner.runs == _RESUMED_RUNS[boundary]
    if boundary == "converged":
        # Nothing left to run, so B trusts A's edges only after observing them.
        release = b.control.releases.get(deployment.release_id)
        assert cluster.fleet.observed[-2:] == [(edge, release.digest) for edge in EDGES]
    reviewed, resumed = journal_for(b.repository, queued.id)
    assert reviewed.id == interrupted.id
    assert reviewed.status is WorkflowStatus.NEEDS_REVIEW
    assert resumed.kind == DEPLOYMENT_WORKFLOW
    assert resumed.status is WorkflowStatus.SUCCEEDED
    assert [step.name for step in resumed.steps] == ["converged"]
    assert actions_for(b.repository, queued.id) == [
        "deployment.queued",
        "deployment.resumed",
        "deployment.succeeded",
    ]

    # Whatever A would write with its old fences is refused at every store.
    targets = b.repository.deployment_targets
    now = datetime.now(UTC)
    for edge in EDGES:
        assert not targets.begin(queued.id, edge, fence=fence_a, now=now)
        assert not targets.record_phase(queued.id, edge, PHASE_ORDER[0], fence=fence_a)
        assert not targets.finish(
            queued.id, edge, fence=fence_a, status=TargetStatus.FAILED, now=now
        )
    assert not b.repository.jobs.complete(
        stranded.job.id, stranded.fence, now=now, result=None
    )
    assert b.control.job_queue.get(taken.job.id) == job
    assert_converged(b.control, cluster.fleet, deployment)


def test_a_worker_that_stalled_past_its_lease_cannot_undo_the_takeover(cluster):
    """A resumes after B finished the same rollout, and every write it tries fails.

    The deployment, targets and job keep B's outcome, and the journal keeps the
    review B asked for rather than A's late failure.
    """
    a, b = cluster.a, cluster.b
    queued = a.control.deployments.submit_deployment("alice")
    stalled: list[Any] = []

    def stall(tags, edges):
        # A stalls before activating the canary, long enough to lose its lease.
        if tags == ("activate",) and "edge-a" in edges and not stalled:
            cluster.clock.expire(claim)
            stalled.append(b.work())

    a.runner.pause = stall
    claim = a.control.job_queue.claim(lease_seconds=1)
    assert claim is not None
    a.control.job_queue.run(claim)

    [taken] = stalled
    deployment = b.control.deployments.get_deployment(queued.id)
    assert deployment.status is DeploymentStatus.SUCCEEDED
    assert_converged(b.control, cluster.fleet, deployment)
    job = b.control.job_queue.get(claim.job.id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.fence == taken.fence > claim.fence
    assert job.last_error is None
    # A's own activation went out after B's; the release is the same one.
    assert a.runner.runs == _phases(("validate", "stage", "activate"), "edge-a")
    stale, resumed = journal_for(b.repository, queued.id)
    assert stale.status is WorkflowStatus.NEEDS_REVIEW
    assert "restarted during external work" in (stale.error or "")
    assert resumed.status is WorkflowStatus.SUCCEEDED
    assert actions_for(b.repository, queued.id) == [
        "deployment.queued",
        "deployment.resumed",
        "deployment.succeeded",
    ]


# -- Interruption until the attempts run out ---------------------------


def test_a_job_interrupted_on_every_attempt_settles_its_deployment_for_review(
    cluster, monkeypatch
):
    a, b = cluster.a, cluster.b
    queued = a.control.deployments.submit_deployment("alice")
    stop_a_at(cluster, "staged", monkeypatch)
    (job,) = a.control.job_queue.list_jobs()

    fences: list[int] = []
    claims = []
    for _ in range(job.max_attempts):
        claims.append(a.die())
        fences.append(a.repository.deployment_targets.generation(queued.id))
        cluster.clock.expire(claims[-1])
    assert [claim.fence for claim in claims] == sorted({c.fence for c in claims})

    # The next claim retires the job instead of handing it out a fourth time.
    assert b.control.job_queue.claim() is None

    retired = b.control.job_queue.get(job.id)
    assert retired.status is JobStatus.FAILED
    assert retired.attempts == job.max_attempts
    assert f"abandoned after {job.max_attempts} attempt(s)" in (
        retired.last_error or ""
    )
    deployment = b.control.deployments.get_deployment(queued.id)
    assert deployment.status is DeploymentStatus.ABANDONED
    assert deployment.finished_at is not None
    assert deployment.result is not None
    assert deployment.result.status is RunStatus.UNSTARTED
    assert f"abandoned after {job.max_attempts} attempt(s)" in (deployment.detail or "")

    # Each attempt's journal is left for an operator, none silently closed.
    journal = journal_for(b.repository, queued.id)
    assert len(journal) == job.max_attempts
    assert {workflow.status for workflow in journal} == {WorkflowStatus.NEEDS_REVIEW}
    assert not any(workflow.steps for workflow in journal)

    # Abandonment took the rollout over, so the last attempt's fence is dead too.
    targets = b.repository.deployment_targets
    assert targets.generation(queued.id) > fences[-1]
    assert not targets.record_phase(
        queued.id, "edge-a", TargetPhase.ACTIVATE, fence=fences[-1]
    )
    canary, rest = targets_of(b.control, queued.id).values()
    assert canary.edge == "edge-a" and canary.phase is TargetPhase.STAGE
    assert rest.phase is None
    # Nothing was activated anywhere: no edge serves anything yet.
    assert cluster.fleet.serving == {}

    assert actions_for(b.repository, queued.id) == [
        "deployment.queued",
        "deployment.resumed",
        "deployment.resumed",
        "deployment.abandoned",
    ]
    # A restart has nothing to re-queue, and the next deployment is unhindered.
    assert b.control.deployments.initialize() == 0
    assert b.control.job_queue.claim() is None
    following = b.control.deployments.submit_deployment("alice")
    b.work()
    deployment = b.control.deployments.get_deployment(following.id)
    assert deployment.status is DeploymentStatus.SUCCEEDED
    assert_converged(b.control, cluster.fleet, deployment)


# -- A refused rollback, then forward reconciliation -------------------


def test_a_rollback_refused_after_takeover_is_reconciled_by_a_forward_deployment(
    cluster, monkeypatch
):
    a, b = cluster.a, cluster.b
    kind = DeploymentRequirementKind.DESIRED_STATE
    requirements = b.repository.deployment_requirements
    good = a.control.deployments.deploy("alice")
    assert good.status is DeploymentStatus.SUCCEEDED
    good_release = a.control.releases.get(good.release_id)
    a.control.dns.create_record(
        DnsRecord(domain="example.com", name="www", value="198.51.100.20"), "alice"
    )
    newer = a.control.deployments.deploy("alice")
    assert newer.status is DeploymentStatus.SUCCEEDED
    assert cluster.fleet.serving == dict.fromkeys(
        EDGES, a.control.releases.get(newer.release_id).digest
    )
    assert not requirements.pending(kind)

    rollback = a.control.deployments.submit_rollback("alice", good.id)
    assert rollback.release_id == good.release_id
    stop_a_at(cluster, "staged", monkeypatch)
    stranded = a.die()

    # An operator edits canonical state while the rollback is in flight.
    late = DnsRecord(
        domain="example.com", name="late", proxied=False, value="198.51.100.77"
    )
    b.control.dns.create_record(late, "bob")

    cluster.clock.expire(stranded)
    taken = b.work()

    refused = b.control.deployments.get_deployment(rollback.id)
    assert refused.status is DeploymentStatus.FAILED
    assert refused.rollback_of == good.id
    # The real run is kept, not a generic abort: the edges did converge.
    assert refused.result is not None
    assert refused.result.status is RunStatus.SUCCEEDED
    assert refused.detail == CANONICAL_CHANGED
    assert_converged(b.control, cluster.fleet, refused)
    assert cluster.fleet.serving == dict.fromkeys(EDGES, good_release.digest)
    # Canonical state was not adopted from the rollback, so both edits stand.
    zones = b.repository.zones
    assert zones.get_record("example.com", "late", RecordType.A) == late
    assert zones.get_record("example.com", "www", RecordType.A).value == "198.51.100.20"
    assert b.control.releases.inputs().digest != good_release.inputs_digest
    assert requirements.pending(kind)
    # A refused rollback is a terminal failure: the job is not retried.
    job = b.control.job_queue.get(taken.job.id)
    assert job.status is JobStatus.FAILED and job.attempts == 2
    reviewed, failed = journal_for(b.repository, rollback.id)
    assert reviewed.status is WorkflowStatus.NEEDS_REVIEW
    assert failed.kind == ROLLBACK_WORKFLOW
    assert failed.status is WorkflowStatus.FAILED
    actions = actions_for(b.repository, rollback.id)
    assert actions[-2:] == ["deployment.failed", "rollback.not_adopted"]
    assert "rollback.applied" not in actions

    # The scheduler's convergence owes the fleet current desired state.
    owed = b.control.deployments.converge_requirements("scheduler")
    assert owed is not None
    assert owed.rollback_of is None and owed.host_limit is None
    assert b.control.deployments.converge_requirements("scheduler") == owed
    b.work()

    forward = b.control.deployments.get_deployment(owed.id)
    assert forward.status is DeploymentStatus.SUCCEEDED
    assert not requirements.pending(kind)
    release = b.control.releases.get(forward.release_id)
    assert release.inputs_digest == b.control.releases.inputs().digest
    assert_converged(b.control, cluster.fleet, forward)
    assert zones.get_record("example.com", "late", RecordType.A) == late
    [journalled] = journal_for(b.repository, forward.id)
    assert journalled.status is WorkflowStatus.SUCCEEDED
    assert b.control.deployments.converge_requirements("scheduler") is None
