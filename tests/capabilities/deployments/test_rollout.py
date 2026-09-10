"""What a rollout guarantees per edge: progress, fencing, partial failure, resume.

These drive the rollout directly rather than through a deployment, because
what is under test is the state machine and not the transaction around it. The
store is real — every guarantee here is a conditional UPDATE naming the fence
it read, and a fake store would be a second implementation of exactly the thing
being checked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from control_plane_fixtures import FakeRunner, ansible_run, host_run

from blitzecdn.capabilities.deployments.domain import (
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.deployments.service.rollout import Rollout
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.capabilities.releases.service import compile_release
from blitzecdn.composition import Repository
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ExecutionError

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

    def verify(self, *, edge: str, address: str, sites):
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

    Empty by default because most of these tests are about the phase machine
    and not about what is served — and an empty release keeps the verifier out
    of the way. `serving=True` puts one hostname in, which is what makes the
    verification phase have something to ask about.
    """
    inputs = release_inputs(serving=serving)
    return compile_release(
        inputs,
        capabilities=("cache", "compression"),
        targets=(),
        contributors=NoContributions(),
        site_checks=lambda site: (),
        allow_empty_sites=True,
    )


@pytest.fixture
def repository(settings):
    return Repository(settings.database_path)


def deployment_in(repository, control_plane_release_id: str) -> str:
    with repository.transaction():
        return repository.deployments.create_deployment(
            "alice", release_id=control_plane_release_id, check_mode=False
        ).id


def rollout_for(repository, *, edges, runner=None, verifier=None, published=None):
    return Rollout(
        targets=repository.deployment_targets,
        runner=runner or FakeRunner(),
        addresses=StubAddresses(edges),
        verifier=verifier or StubVerifier(),
        publish=(published if published is not None else lambda _release: None),
        clock=lambda: _NOW,
    )


def stored(repository, release, inputs: ReleaseInputs | None = None):
    """Put a release and the inputs it names on record.

    The inputs go with it rather than being defaulted: the release row's
    foreign key points at their digest, so saving a release compiled from one
    document beside a different one is a constraint failure — which is the
    schema doing its job.
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
    # Validate in check mode, stage with the tag that touches nothing served,
    # then the real converge.
    assert runner.check_modes == [True, False, False]
    assert runner.tag_selections == [(), ("stage",), ()]


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


def test_staging_is_the_only_phase_that_selects_a_tag(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    rollout = rollout_for(repository, edges={"edge-a": None}, runner=runner)

    rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert [tags for tags in runner.tag_selections if tags] == [("stage",)]


# -- Partial failure -------------------------------------------------------


def test_the_first_failing_edge_stops_the_rollout_and_the_rest_are_skipped(
    repository,
):
    """Skipped, not failed. Those edges are serving what they had.

    Reporting them as failures would send an operator to look at hosts that
    are fine, which is the opposite of what a partial-failure report is for.
    """
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
        edges={"edge-a": None, "edge-b": None, "edge-c": None},
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

    This is the whole reason verification is a phase: `nginx -t` parsed the
    tree and `nginx -s reload` succeeded, and the edge still answers nothing —
    an unreachable upstream, an unclaimed listener, a firewall in front of it.
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


def test_an_edge_with_no_public_address_is_not_probed(repository):
    """An edge nobody has told us how to reach from outside.

    Failing the rollout for it would make declaring a public address
    mandatory, which is a product decision this phase does not get to make.
    """
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    verifier = StubVerifier()
    rollout = rollout_for(repository, edges={"edge-a": None}, verifier=verifier)

    outcome = rollout.converge(deployment, release, edges=["edge-a"], check=False)

    assert outcome.succeeded
    assert verifier.asked == []


def test_a_controller_that_cannot_run_ansible_at_all_stops_the_rollout(repository):
    """Not this edge's failure, and no other edge would fare better."""

    class Broken(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            raise ExecutionError("unable to execute Ansible")

    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    rollout = rollout_for(
        repository, edges={"edge-a": None, "edge-b": None}, runner=Broken()
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
    """Recovery is resumption, and this is what durable progress buys.

    A controller that died mid-rollout used to come back knowing a deployment
    had been abandoned and nothing about which edges were already serving the
    new configuration.
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
    edges = {"edge-a": None, "edge-b": None}

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
    assert runner.host_limits == ["edge-b", "edge-b", "edge-b"]


def test_an_edge_added_since_the_rollout_started_is_picked_up_on_resume(repository):
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    rollout_for(repository, edges={"edge-a": None}).converge(
        deployment, release, edges=["edge-a"], check=False
    )

    rollout_for(repository, edges={"edge-a": None, "edge-b": None}).converge(
        deployment, release, edges=["edge-a", "edge-b"], check=False
    )

    assert {
        target.edge for target in repository.deployment_targets.targets(deployment)
    } == {"edge-a", "edge-b"}


# -- Fencing ---------------------------------------------------------------


def test_a_worker_whose_rollout_was_taken_over_cannot_advance_an_edge(repository):
    """The fence, and the reason a lease alone is not enough.

    A lease decides who *may* converge an edge. This decides whose result about
    that edge is recorded — so a worker that was paused past its lease cannot
    report success for a rollout somebody else has since resumed.
    """
    release = stored(repository, a_release())
    deployment = deployment_in(repository, release.id)
    targets = repository.deployment_targets
    stale = targets.claim_generation(deployment)
    targets.plan(deployment, ["edge-a"], fence=stale)

    # Another worker takes the deployment over: it claims a new generation and
    # stamps every row with it, which is what the first worker's writes will
    # now fail to match.
    fresh = targets.claim_generation(deployment)
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

    Not "it failed" and not "it succeeded" — this worker no longer has any
    claim on that edge, so its opinion about it is worth nothing and recording
    one would overwrite whatever the new owner is doing.
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

    rollout = rollout_for(repository, edges={"edge-a": None}, runner=TakenOverRunner())

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
    from control_plane_fixtures import seed_edge, seed_site

    from blitzecdn.composition import ControlPlane

    repository = Repository(settings.database_path)
    control = ControlPlane(
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
