"""The whole path, once, in the order an operator walks it.

Create a zone, put a hostname on the edge, compile a release, deploy it,
verify the edge serves, break it, and recover. This holds that the steps
compose: a deploy converges the release compiled from the zone just edited,
and a failure partway leaves state an operator can act on.

**Real and not.** The database, compiler, release store, rollout, job queue
and serving probe are production objects. Ansible is a double, so this says
nothing about what happens on an edge; `tests/integration/` and
`just test-integration-http3` cover that on a real machine. The probe is real
and talks to a loopback server this module starts and stops.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import pytest
import yaml
from support.control_plane import (
    ControlPlane,
    FakeRunner,
    ansible_run,
    build_application,
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
from blitzecdn.capabilities.dns.domain import DomainPatch, PatchPolicy
from blitzecdn.capabilities.releases.domain import DESIRED_STATE_ARTIFACT
from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.composition import Repository
from blitzecdn.core.domain.runs import RunStatus

_SERVERS: dict[int, http.server.HTTPServer] = {}


class _Edge(http.server.BaseHTTPRequestHandler):
    """Something that answers on a socket, standing in for a running edge.

    It reports the release the simulated runner activated, since accepting HTTP
    proves no release identity.
    """

    def do_GET(self) -> None:
        payload = (self.server.release_digest + "\n").encode("ascii")
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


class _EdgeProcess:
    """A listener that can be taken away, which is what "stops serving" means.

    Not a status code: the probe passes a 502 or 503, which is the edge reporting
    on something behind it. Verification catches an edge answering nothing.
    """

    def __init__(self) -> None:
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Edge)
        self.port = self._server.server_address[1]
        self._server.release_digest = "untracked"
        self._server.freeze_release = False
        _SERVERS[self.port] = self._server
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        _SERVERS.pop(self.port, None)
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
    """The production probe, aimed at a loopback port instead of 80."""

    class LoopbackProbe(ServingProbe):
        def _connection(self, scheme, address, _port, hostname):
            return super()._connection(scheme, address, port, hostname)

    return LoopbackProbe(timeout=5.0)


def control_plane(settings, port: int, runner: FakeRunner) -> ControlPlane:
    # Simulate the runner's activation effect, separately from the real probe.
    # Merely publishing or validating a release must not update the listener.
    original_run = runner.run
    server = _SERVERS[port]

    def run(*, check, host_limit=None, tags=()):
        result = original_run(check=check, host_limit=host_limit, tags=tags)
        if (
            result.succeeded
            and not check
            and tags == ("activate",)
            and not server.freeze_release
        ):
            document = yaml.safe_load(settings.generated_vars_path.read_text())
            server.release_digest = document["blitzecdn_release_digest"]
        return result

    runner.run = run
    return build_application(
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

    # 1. An edge, and a zone with a hostname on it, seeded through the shared helper.
    # The origin is the record's own value.
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

    Everything Ansible reports is fine, and the edge answers 503 to the request
    that matters.
    """
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = control_plane(settings, edge.port, runner)
    seed_edge(control, name="edge-a", public_address="127.0.0.1")
    seed_site(control, ssl_mode=SslMode.OFF)
    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED

    # The edge stops listening, while Ansible already said every task was fine.
    edge.stop()
    control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(cache_valid_success="1h")),
        "alice",
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

    Converging the old artifact alone would let the next reconciliation undo the
    rollback.
    """
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = control_plane(settings, edge_port, runner)
    seed_edge(control, name="edge-a", public_address="127.0.0.1")
    seed_site(control, ssl_mode=SslMode.OFF)
    good = control.deployments.deploy("alice")

    # A change that turns out to be wrong, deployed.
    control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(always_use_https=True)),
        "alice",
    )
    bad = control.deployments.deploy("alice")
    assert bad.release_id != good.release_id
    assert control.dns.get_domain("example.com").policy.tls.always_use_https is True

    rolled_back = control.deployments.rollback("alice", good.id)

    assert rolled_back.status is DeploymentStatus.SUCCEEDED
    # Canonical state came back with the old release, so compiling now produces
    # the release rolled back to.
    assert control.dns.get_domain("example.com").policy.tls.always_use_https is False
    assert control.releases.compile().id == good.release_id


def test_a_rollout_interrupted_partway_resumes_rather_than_restarting(
    settings, edge_port
):
    """A rollout that stopped mid-fleet: what the edges already have is on record.

    This retries the rollout after a reported failure; worker crash recovery is in
    the deployment workflow suite.
    """

    class OneBadEdge(FakeRunner):
        """Every edge converges except `edge-b`, which cannot be reached."""

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

    # Retry the failed rollout primitive: the successful edge is re-observed
    # and only the remaining edge runs Ansible again.
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
        "edge-b",
    ]


def test_a_healthy_edge_that_did_not_activate_the_new_release_fails_verification(
    settings, edge
):
    runner = FakeRunner()
    control = control_plane(settings, edge.port, runner)
    seed_edge(control, name="edge-a", public_address="127.0.0.1")
    seed_site(control, ssl_mode=SslMode.OFF)
    good = control.deployments.deploy("alice")
    assert good.status is DeploymentStatus.SUCCEEDED
    edge._server.freeze_release = True
    control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(cache_valid_success="1h")),
        "alice",
    )

    stale = control.deployments.deploy("alice")

    assert stale.status is DeploymentStatus.FAILED
    assert stale.result.succeeded  # preserve what the execution tool actually reported
    assert "expected release" in stale.error
    assert "expected release" in stale.detail
    assert edge._server.release_digest == control.releases.get(good.release_id).digest
