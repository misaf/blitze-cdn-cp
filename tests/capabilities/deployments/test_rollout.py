"""What a rollout guarantees per edge: progress, fencing, partial failure, resume.

These drive the rollout state machine directly. The store is real: every
guarantee is a conditional UPDATE naming the fence it read, which a fake would
reimplement.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from support.control_plane import FakeRunner, ansible_run, build_application, host_run
from support.stores import Stores

from blitzecdn.capabilities.deployments.domain import (
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.deployments.service.rollout import Rollout
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.capabilities.releases.service import compile_release
from blitzecdn.capabilities.releases.service.preparation import prepare_compilation
from blitzecdn.capabilities.sites.domain import DnsRecord, Domain
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ConflictError, ExecutionError

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class StubAddresses:
    """The fleet, without a store or a database behind it."""

    def __init__(self, edges: dict[str, str | None]) -> None:
        self.edges = edges

    def selected(self, host_limit: str | None) -> list[str]:
        if host_limit is None:
            return list(self.edges)
        return [name for name in self.edges if name == host_limit]

    def public_address(self, edge: str) -> str | None:
        return self.edges.get(edge)


class StubVerifier:
    def __init__(self, *, served: bool = True) -> None:
        self.served = served
        self.asked: list[str] = []

    def verify(self, *, edge: str, address: str, sites, release_digest: str):
        self.asked.append(edge)
        return [
            type(
                "Result",
                (),
                {
                    "edge": edge,
                    "hostname": site.server_names[0],
                    "served": self.served,
                    "detail": "HTTP 200" if self.served else "refused",
                },
            )()
            for site in sites
            if site.server_names
        ]


class NoContributions:
    def site_variables(self, site):
        return {"name": site.name}

    def fleet_variables(self, sites):
        return {}


def release_inputs(*, serving: bool) -> ReleaseInputs:
    if not serving:
        return ReleaseInputs()
    return ReleaseInputs.of(
        [Domain(name="example.com")],
        [DnsRecord(domain="example.com", name="cdn", value="198.51.100.10")],
        [],
    )


def a_release(*, serving: bool = False):
    """A compiled release, optionally with a hostname in it to verify.

    Empty by default, keeping the verifier out of the way; `serving=True` gives
    the verification phase something to ask about.
    """
    inputs = release_inputs(serving=serving)
    return compile_release(
        prepare_compilation(
            inputs,
            capabilities=("cache", "compression"),
            targets=(),
            contributors=NoContributions(),
            site_checks=lambda site: (),
            allow_empty_sites=True,
        )
    )


@pytest.fixture
def repository(settings):
    return Stores(settings.database_path)


def deployment_in(repository, control_plane_release_id: str) -> str:
    with repository.transaction():
        return repository.deployments.create_deployment(
            "alice", release_id=control_plane_release_id, check_mode=False
        ).id


def rollout_for(
    repository, *, edges, runner=None, verifier=None, published=None, batch_size=10
):
    return Rollout(
        targets=repository.deployment_targets,
        runner=runner or FakeRunner(),
        addresses=StubAddresses(edges),
        verifier=verifier or StubVerifier(),
        publish=(published if published is not None else lambda _release: None),
        clock=lambda: _NOW,
        batch_size=batch_size,
    )


class PerHostRunner(FakeRunner):
    """Answers each run per host, the way Runner's events do.

    ``failing`` maps a tag selection to the edges that fail it. Under the play's
    ``any_errors_fatal`` the rest of the run still reports, unfailed, and the run
    as a whole fails.
    """

    def __init__(self, failing: dict[tuple[str, ...], set[str]] | None = None):
        super().__init__()
        self.failing = failing or {}

    def run(self, *, check, host_limit=None, tags=()):
        super().run(check=check, host_limit=host_limit, tags=tags)
        edges = (host_limit or "").split(",")
        bad = self.failing.get(tags, set()) & set(edges)
        return ansible_run(
            *(
                host_run(edge, failure="refused" if edge in bad else None)
                for edge in edges
            ),
            status=RunStatus.FAILED if bad else RunStatus.SUCCEEDED,
            return_code=2 if bad else 0,
            targeted=tuple(edges),
        )


def fleet(count: int) -> dict[str, str]:
    return {f"edge-{index}": f"192.0.2.{10 + index}" for index in range(count)}


def stored(repository, release, inputs: ReleaseInputs | None = None):
    """Put a release and the inputs it names on record.

    The release row's foreign key points at the inputs' digest.
    """
    with repository.transaction():
        repository.releases.save(release, inputs or ReleaseInputs())
    return release


# -- Phases ----------------------------------------------------------------


def test_every_edge_walks_the_five_phases_in_order(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    rollout = rollout_for(repository, edges={"edge-a": "203.0.113.10"}, runner=runner)

    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert outcome.succeeded
    (target,) = outcome.targets
    assert target.phase is TargetPhase.VERIFY
    # Preview, stage a sealed candidate, activate, then commit only after the
    # external serving observation.
    assert runner.check_modes == [True, False, False, False]
    assert runner.tag_selections == [(), ("stage",), ("activate",), ("commit",)]


def test_a_check_mode_rollout_stops_after_validating(repository):
    """A drift check must never stage an image or reload a configuration."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    rollout = rollout_for(repository, edges={"edge-a": "203.0.113.10"}, runner=runner)

    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=True)

    assert outcome.succeeded
    assert outcome.targets[0].phase is TargetPhase.VALIDATE
    assert runner.check_modes == [True]
    assert runner.tag_selections == [()]


def test_each_mutation_selects_its_explicit_phase(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    rollout = rollout_for(repository, edges={"edge-a": "192.0.2.10"}, runner=runner)

    rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert [tags for tags in runner.tag_selections if tags] == [
        ("stage",),
        ("activate",),
        ("commit",),
    ]


# -- Partial failure -------------------------------------------------------


def test_the_first_failing_edge_stops_the_rollout_and_the_rest_are_skipped(
    repository,
):
    """Skipped, not failed. Those edges are serving what they had."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    runner = FakeRunner(
        [
            ansible_run(
                host_run("edge-a", failed=1, failure="nginx -t refused it"),
                status=RunStatus.FAILED,
                return_code=2,
            )
        ]
    )
    rollout = rollout_for(
        repository,
        edges={"edge-a": "192.0.2.10", "edge-b": "192.0.2.11", "edge-c": "192.0.2.12"},
        runner=runner,
    )

    outcome = rollout.converge(
        deployment, release, edges=["edge-a", "edge-b", "edge-c"], check=False
    )

    assert not outcome.succeeded
    progress = {target.edge: target.status for target in outcome.targets}
    assert progress == {
        "edge-a": TargetStatus.FAILED,
        "edge-b": TargetStatus.SKIPPED,
        "edge-c": TargetStatus.SKIPPED,
    }
    assert outcome.unattempted == ("edge-b", "edge-c")
    assert outcome.converged == ()


def test_an_edge_that_accepted_the_configuration_but_serves_nothing_fails(
    repository,
):
    """A reload that returned zero is not evidence that a visitor gets a reply.

    That is why verification is a phase: an unreachable upstream, an unclaimed
    listener or a firewall can leave the edge answering nothing.
    """
    release = stored(repository, a_release(serving=True), release_inputs(serving=True))
    deployment = deployment_in(repository, release.id)
    rollout = rollout_for(
        repository,
        edges={"edge-a": "203.0.113.10"},
        verifier=StubVerifier(served=False),
    )

    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert not outcome.succeeded
    (target,) = outcome.targets
    assert target.status is TargetStatus.FAILED
    assert "verify failed" in (target.last_error or "")


def test_an_edge_with_no_public_address_cannot_be_verified(repository):
    """Unperformed verification must never be recorded as success."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    verifier = StubVerifier()
    rollout = rollout_for(repository, edges={"edge-a": None}, verifier=verifier)

    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert not outcome.succeeded
    assert verifier.asked == []


def test_a_controller_that_cannot_run_ansible_at_all_stops_the_rollout(repository):
    """Not this edge's failure, and no other edge would fare better."""

    class Broken(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            raise ExecutionError("unable to execute Ansible")

    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    rollout = rollout_for(
        repository,
        edges={"edge-a": "192.0.2.10", "edge-b": "192.0.2.11"},
        runner=Broken(),
    )

    with pytest.raises(ExecutionError):
        rollout.converge(deployment, release, edges=["edge-a", "edge-b"], check=False)

    progress = {
        target.edge: target.status
        for target in repository.deployment_targets.targets(deployment)
    }
    assert progress["edge-a"] is TargetStatus.FAILED


# -- Resumption ------------------------------------------------------------


def test_an_interrupted_rollout_resumes_from_the_edges_it_had_not_reached(
    repository,
):
    """Run again for the same deployment, the rollout skips converged edges.

    Worker recovery uses the same state machine, re-observing a completed edge
    before trusting its checkpoint.
    """
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    failing = FakeRunner(
        [
            ansible_run(
                host_run("edge-b", failed=1, failure="unreachable"),
                status=RunStatus.FAILED,
                return_code=2,
            )
        ]
    )
    edges = {"edge-a": "192.0.2.10", "edge-b": "192.0.2.11"}

    # First attempt: edge-a converges, edge-b fails.
    first = rollout_for(repository, edges=edges, runner=FakeRunner())
    first.converge(deployment, release, edges=["edge-a"], check=False)
    stopped = rollout_for(repository, edges=edges, runner=failing)
    stopped.converge(deployment, release, edges=["edge-a", "edge-b"], check=False)

    # A second attempt does not re-converge the edge that already succeeded.
    runner = FakeRunner()
    resumed = rollout_for(repository, edges=edges, runner=runner)
    outcome = resumed.converge(
        deployment, release, edges=["edge-a", "edge-b"], check=False
    )

    assert outcome.succeeded
    assert runner.host_limits == ["edge-b", "edge-b", "edge-b", "edge-b"]


def test_an_edge_added_since_the_rollout_started_is_picked_up_on_resume(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    rollout_for(repository, edges={"edge-a": "192.0.2.10"}).converge(
        deployment, release, edges=["edge-a"], check=False
    )

    rollout_for(
        repository, edges={"edge-a": "192.0.2.10", "edge-b": "192.0.2.11"}
    ).converge(deployment, release, edges=["edge-a", "edge-b"], check=False)

    assert {
        target.edge for target in repository.deployment_targets.targets(deployment)
    } == {"edge-a", "edge-b"}


# -- Fencing ---------------------------------------------------------------


def test_a_worker_whose_rollout_was_taken_over_cannot_advance_an_edge(repository):
    """The fence, and the reason a lease alone is not enough.

    A lease decides who may converge an edge; the fence decides whose result is
    recorded, so a worker paused past its lease cannot report success.
    """
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    targets = repository.deployment_targets
    stale = targets.claim_generation(deployment)
    targets.plan(deployment, ["edge-a"], fence=stale)

    # Another worker takes over: it claims a new generation and stamps every row,
    # so the first worker's writes no longer match.
    fresh = targets.claim_generation(deployment)
    # Takeover is effective before the new worker plans or executes anything.
    assert targets.begin(deployment, "edge-a", fence=stale, now=_NOW) is False
    with pytest.raises(ConflictError, match="stale generation"):
        targets.plan(deployment, ["edge-a"], fence=stale)
    targets.plan(deployment, ["edge-a"], fence=fresh)
    assert fresh > stale

    assert targets.begin(deployment, "edge-a", fence=stale, now=_NOW) is False
    assert (
        targets.record_phase(deployment, "edge-a", TargetPhase.ACTIVATE, fence=stale)
        is False
    )
    assert (
        targets.finish(
            deployment,
            "edge-a",
            fence=stale,
            status=TargetStatus.SUCCEEDED,
            now=_NOW,
        )
        is False
    )
    # And the worker that did take it over can.
    assert targets.begin(deployment, "edge-a", fence=fresh, now=_NOW) is True


def test_a_rollout_that_lost_its_claim_stops_rather_than_reporting(repository):
    """A worker that was taken over mid-rollout reports nothing about its edge.

    Recording anything would overwrite whatever the new owner is doing.
    """
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    targets = repository.deployment_targets

    class TakenOverRunner(FakeRunner):
        """Another worker claims the deployment while this one is converging."""

        def run(self, *, check, host_limit=None, tags=()):
            targets.plan(
                deployment, ["edge-a"], fence=targets.claim_generation(deployment)
            )
            return super().run(check=check, host_limit=host_limit, tags=tags)

    rollout = rollout_for(
        repository, edges={"edge-a": "192.0.2.10"}, runner=TakenOverRunner()
    )

    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert not outcome.succeeded
    assert "taken over by another worker" in (outcome.error or "")
    # And no result was recorded for the edge by the worker that lost it.
    (target,) = targets.targets(deployment)
    assert target.status is not TargetStatus.SUCCEEDED


# -- Reporting -------------------------------------------------------------


def test_a_deployment_records_which_edges_converged_and_which_were_not_attempted(
    settings,
):
    from support.control_plane import seed_edge, seed_site

    repository = Stores(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", failed=1, failure="refused"),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            ]
        ),
    )
    seed_edge(control, name="edge-a")
    seed_edge(control, name="edge-b", host="192.0.2.11")
    seed_site(control)

    deployment = control.deployments.deploy("alice")

    assert deployment.status is DeploymentStatus.FAILED
    recorded = next(
        event
        for event in repository.audit_log.list_audit_events(10)
        if event.action == "deployment.failed"
    )
    assert recorded.details["unattempted"] == ["edge-b"]
    assert recorded.details["converged"] == []


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_serving_verification_withdraws_the_pending_activation(
    repository, rollback_fails
):
    class Runtime(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            result = super().run(check=check, host_limit=host_limit, tags=tags)
            if tags == ("rollback",) and rollback_fails:
                return ansible_run(
                    host_run("edge-a", failed=1, failure="previous image unavailable"),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            return result

    release = stored(repository, a_release(serving=True), release_inputs(serving=True))
    deployment = deployment_in(repository, release.id)
    runner = Runtime()
    rollout = rollout_for(
        repository,
        edges={"edge-a": "203.0.113.10"},
        runner=runner,
        verifier=StubVerifier(served=False),
    )
    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)
    assert not outcome.succeeded
    assert runner.tag_selections == [(), ("stage",), ("activate",), ("rollback",)]
    assert outcome.run.succeeded, "the activation tool result remains independent"
    if rollback_fails:
        assert "rollback failed" in outcome.error
        assert "previous configuration restored" not in outcome.error
    else:
        assert "previous configuration restored" in outcome.error


def test_failed_activation_is_withdrawn_before_the_rollout_stops(repository):
    class Runtime(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            result = super().run(check=check, host_limit=host_limit, tags=tags)
            if tags == ("activate",):
                return ansible_run(
                    host_run("edge-a", failed=1, failure="reload refused"),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            return result

    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    runner = Runtime()
    rollout = rollout_for(repository, edges={"edge-a": "203.0.113.10"}, runner=runner)
    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)
    assert not outcome.succeeded
    assert runner.tag_selections[-1] == ("rollback",)
    assert ("commit",) not in runner.tag_selections
    assert outcome.targets[0].phase is TargetPhase.STAGE
    assert not outcome.run.succeeded


# -- Pacing ----------------------------------------------------------------


def test_the_canary_goes_alone_and_the_rest_go_in_batches(repository):
    """One run per phase per batch, not one per edge."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(4)
    runner = PerHostRunner()
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)

    outcome = rollout.converge(deployment, release, edges=list(edges), check=False)

    assert outcome.succeeded
    assert runner.host_limits == [
        *["edge-0"] * 4,
        *["edge-1,edge-2"] * 4,
        *["edge-3"] * 4,
    ]


def test_a_check_mode_rollout_needs_no_canary_and_reports_every_edge(repository):
    """The drift report reads the record, so it must hold every batch's hosts."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(3)
    runner = PerHostRunner()
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)

    outcome = rollout.converge(deployment, release, edges=list(edges), check=True)

    assert outcome.succeeded
    assert runner.host_limits == ["edge-0,edge-1", "edge-2"]
    assert [host.host for host in outcome.run.hosts] == list(edges)
    assert outcome.run.unattempted == ()


def test_a_batch_is_cut_where_its_names_would_not_fit_one_host_limit(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = {f"{letter * 200}": "192.0.2.10" for letter in "abcd"}
    runner = PerHostRunner()
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=10)

    rollout.converge(deployment, release, edges=list(edges), check=True)

    assert [len(limit.split(",")) for limit in runner.host_limits] == [2, 2]
    assert all(len(limit) <= 512 for limit in runner.host_limits)


def test_a_resumed_rollout_whose_canary_passed_goes_straight_to_batches(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(3)
    rollout_for(repository, edges={"edge-0": edges["edge-0"]}).converge(
        deployment, release, edges=["edge-0"], check=False
    )

    runner = PerHostRunner()
    rollout_for(repository, edges=edges, runner=runner, batch_size=5).converge(
        deployment, release, edges=list(edges), check=False
    )

    assert set(runner.host_limits) == {"edge-1,edge-2"}


def test_a_validation_failure_in_a_batch_skips_the_edges_stopped_with_it(
    repository,
):
    """Edges the play stopped because another failed were never changed."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(5)
    runner = PerHostRunner({(): {"edge-2"}})
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)

    outcome = rollout.converge(deployment, release, edges=list(edges), check=False)

    assert not outcome.succeeded
    assert outcome.error.startswith("edge-2: validate failed: edge-2:")
    assert {t.edge: t.status for t in outcome.targets} == {
        "edge-0": TargetStatus.SUCCEEDED,
        "edge-1": TargetStatus.SKIPPED,
        "edge-2": TargetStatus.FAILED,
        "edge-3": TargetStatus.SKIPPED,
        "edge-4": TargetStatus.SKIPPED,
    }
    stopped = next(t for t in outcome.targets if t.edge == "edge-1")
    assert "stopped because edge-2 failed" in (stopped.last_error or "")
    assert ("stage",) not in runner.tag_selections[4:]


def test_an_activation_failure_in_a_batch_withdraws_every_edge_it_stopped(
    repository,
):
    """Collateral edges stopped mid-activation are in an unknown state: restore."""
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(3)
    runner = PerHostRunner({("activate",): {"edge-1"}})
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)

    outcome = rollout.converge(deployment, release, edges=list(edges), check=False)

    assert not outcome.succeeded
    assert outcome.error.startswith("edge-1: activate failed")
    statuses = {t.edge: t.status for t in outcome.targets}
    assert statuses["edge-1"] is TargetStatus.FAILED
    assert statuses["edge-2"] is TargetStatus.FAILED
    rollbacks = [
        limit
        for limit, tags in zip(runner.host_limits, runner.tag_selections, strict=True)
        if tags == ("rollback",)
    ]
    assert rollbacks == ["edge-1", "edge-2"]
    assert ("commit",) not in runner.tag_selections[4:]


def test_an_edge_failing_verification_does_not_hold_back_its_batch(repository):
    """Edges already serving the release and answering for it are committed."""

    class OneDeaf(StubVerifier):
        def verify(self, *, edge, address, sites, release_digest):
            results = super().verify(
                edge=edge, address=address, sites=sites, release_digest=release_digest
            )
            if edge == "edge-1":
                for result in results:
                    result.served = False
            return results

    release = stored(repository, a_release(serving=True), release_inputs(serving=True))
    deployment = deployment_in(repository, release.id)
    edges = fleet(4)
    runner = PerHostRunner()
    rollout = rollout_for(
        repository, edges=edges, runner=runner, verifier=OneDeaf(), batch_size=2
    )

    outcome = rollout.converge(deployment, release, edges=list(edges), check=False)

    assert not outcome.succeeded
    assert {t.edge: t.status for t in outcome.targets} == {
        "edge-0": TargetStatus.SUCCEEDED,
        "edge-1": TargetStatus.FAILED,
        "edge-2": TargetStatus.SUCCEEDED,
        "edge-3": TargetStatus.SKIPPED,
    }
    calls = list(zip(runner.host_limits, runner.tag_selections, strict=True))
    assert ("edge-2", ("commit",)) in calls
    assert ("edge-1", ("rollback",)) in calls


def test_a_check_stands_aside_between_batches_for_a_waiting_deployment(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(5)
    runner = PerHostRunner()
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)
    asked: list[int] = []

    def waiting() -> bool:
        asked.append(len(runner.host_limits))
        return len(runner.host_limits) >= 1

    outcome = rollout.converge(
        deployment, release, edges=list(edges), check=True, should_yield=waiting
    )

    assert outcome.yielded
    assert not outcome.succeeded
    assert runner.host_limits == ["edge-0,edge-1"]
    assert outcome.unattempted == ("edge-2", "edge-3", "edge-4")
    assert "stood aside" in (outcome.error or "")


def test_an_edge_stopped_during_another_edges_commit_is_committed_on_its_own(
    repository,
):
    """A committed journal cannot be rolled back, so it is never withdrawn."""

    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(3)
    runner = PerHostRunner({("commit",): {"edge-1"}})
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)

    outcome = rollout.converge(deployment, release, edges=list(edges), check=False)

    statuses = {t.edge: t.status for t in outcome.targets}
    assert statuses["edge-1"] is TargetStatus.FAILED
    assert statuses["edge-2"] is TargetStatus.SUCCEEDED
    calls = list(zip(runner.host_limits, runner.tag_selections, strict=True))
    assert ("edge-2", ("commit",)) in calls
    assert ("edge-2", ("rollback",)) not in calls


def test_an_unexpected_error_in_a_phase_withdraws_and_closes_the_batch(repository):
    """Not an ExecutionError: the play may already have activated, so restore."""

    class DiesAfterActivating(PerHostRunner):
        def run(self, *, check, host_limit=None, tags=()):
            result = super().run(check=check, host_limit=host_limit, tags=tags)
            if tags == ("activate",) and "," in (host_limit or ""):
                raise OSError("could not keep the run output")
            return result

    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    edges = fleet(3)
    runner = DiesAfterActivating()
    rollout = rollout_for(repository, edges=edges, runner=runner, batch_size=2)

    outcome = rollout.converge(deployment, release, edges=list(edges), check=False)

    assert not outcome.succeeded
    assert "OSError" in (outcome.error or "")
    statuses = {t.edge: t.status for t in outcome.targets}
    assert statuses["edge-1"] is TargetStatus.FAILED
    assert statuses["edge-2"] is TargetStatus.FAILED
    rollbacks = [
        limit
        for limit, tags in zip(runner.host_limits, runner.tag_selections, strict=True)
        if tags == ("rollback",)
    ]
    assert rollbacks == ["edge-1", "edge-2"]
