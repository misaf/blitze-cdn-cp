"""The whole path, once, in the order an operator walks it.

Create a zone, point it at an origin, put a hostname on the edge, compile a
release, deploy it, verify the edge is serving, break it, and recover. Every
one of those has focused tests elsewhere; what this holds is that they compose
— that the release a deploy converges is the one the compiler made from the
zone that was just edited, and that a failure partway through leaves state an
operator can act on.

**What is real here and what is not.** The database, the compiler, the release
store, the per-edge rollout, the job queue and the serving probe are the
production objects. Ansible is not: the runner is a double, because a real one
needs an Ubuntu 26.04 host with Docker on it and this suite runs on a laptop.
So this proves the control plane's half of the path end to end and proves
nothing about what Ansible does on an edge — `tests/integration/` and
`just test-integration-http3` are where that lives, and they are honest about
needing a machine.

The serving probe is the exception and is deliberately real: verification's
whole claim is that something answered over a socket, so this starts a server,
lets the probe find it, and then takes it away.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import pytest
from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    host_run,
    seed_edge,
    seed_site,
)

from blitzecdn.capabilities.deployments.adapters.serving import ServingProbe
from blitzecdn.capabilities.deployments.domain import (
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.releases.domain import DESIRED_STATE_ARTIFACT
from blitzecdn.capabilities.tls.policy import SslMode
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.domain.runs import RunStatus


class _Edge(http.server.BaseHTTPRequestHandler):
    """Something that answers on a socket, standing in for a running edge.

    Not an nginx. What the verification phase asks is "does this address answer
    for this hostname", and that question has the same shape whatever is behind
    it — which is exactly why the phase is worth having: the control plane
    cannot tell what is listening, and should not pretend to.
    """

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


class _EdgeProcess:
    """A listener that can be taken away, which is what "stops serving" means.

    Not a status code. A 502 or a 503 is the edge working and reporting on
    something behind it, and the probe passes both on purpose — an origin that
    was already down was not this deployment's doing. The failure verification
    exists to catch is the edge answering *nothing*, so that is what this can
    do.
    """

    def __init__(self) -> None:
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Edge)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)


@pytest.fixture
def edge() -> Iterator[_EdgeProcess]:
    process = _EdgeProcess()
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
def edge_port(edge: _EdgeProcess) -> int:
    return edge.port


def probe_on(port: int) -> ServingProbe:
    """The production probe, aimed at a loopback port instead of 80.

    Overriding the port is the one concession: in production it comes from the
    site's scheme, which is the contract and is not what this is testing.
    """

    class LoopbackProbe(ServingProbe):
        def _connection(self, scheme, address, _port, hostname):
            return super()._connection(scheme, address, port, hostname)

    return LoopbackProbe(timeout=5.0)


def control_plane(settings, port: int, runner: FakeRunner) -> ControlPlane:
    return ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=runner,
        serving_probe=probe_on(port),
    )


def test_the_whole_path_from_an_empty_control_plane_to_a_verified_edge(
    settings, edge_port
):
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = control_plane(settings, edge_port, runner)

    # 1. An edge, and a zone with a hostname on it. Seeded through the shared
    # helper so the site is one *this* workspace can serve: in the core-only
    # run the optional settings are off, and the path being walked is the same
    # either way. The origin is the record's own value.
    seed_edge(control, name="edge-a", public_address="127.0.0.1")

    # 2. The policy the zone serves that hostname by.
    seed_site(
        control,
        origin="198.51.100.10",
        record="cdn",
        ssl_mode=SslMode.OFF,
        cache_valid_success="1h",
    )

    # 3. A release compiled from exactly that state, and it explains itself.
    release = control.releases.compile()
    assert release.servable
    (compiled,) = release.sites
    assert compiled.site.server_names == ("cdn.example.com",)
    assert compiled.site.origin_host == "198.51.100.10"
    settings_by_name = {item.setting: item for item in release.explanations[0].settings}
    assert settings_by_name["cache_valid_success"].origin.value == "zone"
    # And a setting nobody touched is attributed to the schema, which is the
    # distinction an operator most often mistakes for one they made.
    assert settings_by_name["minimum_tls_version"].origin.value == "default"

    # 4. Deploying converges that release, edge by edge, and verifies it.
    deployment = control.deployments.deploy("alice")

    assert deployment.status is DeploymentStatus.SUCCEEDED
    assert deployment.release_id == release.id
    (target,) = control.deployments.targets(deployment.id)
    assert target.status is TargetStatus.SUCCEEDED
    assert target.phase is TargetPhase.VERIFY
    # The document on disk is the release's own bytes.
    assert (
        release.artifact(DESIRED_STATE_ARTIFACT).document["blitzecdn_nginx_sites"][0][
            "name"
        ]
        == "example-com"
    )
    assert settings.generated_vars_path.exists()


def test_an_edge_that_stops_serving_fails_the_deployment_that_touched_it(
    settings, edge
):
    """A reload that returned zero is not a fleet that is serving.

    This is the failure the whole verification phase exists for, walked the
    long way round: everything Ansible reports is fine, and the edge answers
    503 to the one request that matters.
    """
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = control_plane(settings, edge.port, runner)
    seed_edge(control, name="edge-a", public_address="127.0.0.1")
    seed_site(control, ssl_mode=SslMode.OFF)
    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED

    # The edge stops listening — a container that died, a listener that never
    # claimed the port, a firewall that closed in front of it. Ansible knows
    # none of that; it already said every task was fine.
    edge.stop()
    control.dns.update_domain(
        "example.com", DomainPatch(cache_valid_success="1h"), "alice"
    )
    broken = control.deployments.deploy("alice")

    # Ansible said every task was fine. The edge did not answer.
    assert runner.results[0].succeeded
    assert broken.status is DeploymentStatus.FAILED
    (target,) = control.deployments.targets(broken.id)
    assert target.status is TargetStatus.FAILED
    assert "verify failed" in (target.last_error or "")
    assert "not serving" in (target.last_error or "")


def test_a_failed_update_is_recovered_by_rolling_back_the_desired_target(
    settings, edge_port
):
    """Rollback is a change of desired state, not a temporary override.

    Converging the old artifact alone would leave the edges on one state and
    the control plane asserting another — and the next reconciliation would
    compile what the control plane still held and quietly undo the rollback.
    """
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = control_plane(settings, edge_port, runner)
    seed_edge(control, name="edge-a", public_address="127.0.0.1")
    seed_site(control, ssl_mode=SslMode.OFF)
    good = control.deployments.deploy("alice")

    # A change that turns out to be wrong, deployed.
    control.dns.update_domain(
        "example.com", DomainPatch(always_use_https=True), "alice"
    )
    bad = control.deployments.deploy("alice")
    assert bad.release_id != good.release_id
    assert control.dns.get_domain("example.com").always_use_https is True

    rolled_back = control.deployments.rollback("alice", good.id)

    assert rolled_back.status is DeploymentStatus.SUCCEEDED
    # The edges converged the old release *and* canonical state came back with
    # it, so compiling now produces the release that was rolled back to rather
    # than the one that was rolled back from.
    assert control.dns.get_domain("example.com").always_use_https is False
    assert control.releases.compile().id == good.release_id


def test_a_rollout_interrupted_partway_resumes_rather_than_restarting(
    settings, edge_port
):
    """The controller died mid-fleet. What the edges already have is on record."""

    class OneBadEdge(FakeRunner):
        """Every edge converges except `edge-b`, which cannot be reached.

        Per edge rather than per call, because that is the shape of the failure
        being modelled: one host in a fleet is down, and the rollout has to get
        that far before it finds out.
        """

        def run(self, *, check, host_limit=None, tags=()):
            super().run(check=check, host_limit=host_limit, tags=tags)
            if host_limit == "edge-b":
                return ansible_run(
                    host_run("edge-b", failed=1, failure="unreachable"),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            return ansible_run(host_run(host_limit or "edge-a"))

    control = control_plane(settings, edge_port, OneBadEdge())
    for name, host in (("edge-a", "127.0.0.1"), ("edge-b", "127.0.0.1")):
        seed_edge(control, name=name, host=host, public_address=host)
    seed_site(control, ssl_mode=SslMode.OFF)

    stopped = control.deployments.deploy("alice")
    assert stopped.status is DeploymentStatus.FAILED
    progress = {
        target.edge: target.status for target in control.deployments.targets(stopped.id)
    }
    assert progress["edge-a"] is TargetStatus.SUCCEEDED
    assert progress["edge-b"] is TargetStatus.FAILED

    # The operator fixes the edge and the same deployment is resumed. The edge
    # that already converged is not touched again.
    control.deployments.execution.rollout.runner = FakeRunner()
    control.deployments.execution.rollout.converge(
        stopped.id,
        control.releases.get(stopped.release_id),
        edges=["edge-a", "edge-b"],
        check=False,
    )

    assert control.deployments.execution.rollout.runner.host_limits == [
        "edge-b",
        "edge-b",
        "edge-b",
    ]
