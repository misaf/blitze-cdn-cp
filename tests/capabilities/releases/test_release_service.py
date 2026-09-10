"""Recording a release, and what a deployment does with the one it named.

The compiler's own tests use no database. These use the real one, because what
is under test here is the part the compiler refuses to do: that recording is
idempotent by digest, that pruning cannot remove a release the fleet can still
be sent back to, and that a deployment converges the exact document its release
carries rather than re-deriving one from state that has moved on.
"""

from __future__ import annotations

import pytest
from control_plane_fixtures import FakeRunner, seed_record, seed_site

from blitzecdn.capabilities.dns.domain import Domain, DomainPatch, RecordType
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.releases.domain import (
    DESIRED_STATE_ARTIFACT,
    UnservableReleaseError,
)
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.exceptions import NotFoundError


@pytest.fixture
def control(settings):
    repository = Repository(settings.database_path)
    return ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    ), repository


def test_compiling_records_nothing(control):
    """`blitzecdn validate` compiles on every invocation.

    An operator fixing a fleet runs it repeatedly, and a compiler that recorded
    each attempt would turn a habit into a history of who asked rather than of
    what the fleet was asked to serve.
    """
    plane, _ = control
    seed_record(plane)

    plane.releases.compile()

    assert plane.releases.list_releases() == []


def test_preparing_the_same_state_twice_records_one_release(control):
    plane, _ = control
    seed_record(plane)

    first = plane.releases.prepare()
    second = plane.releases.prepare()

    assert first.digest == second.digest
    assert [item.id for item in plane.releases.list_releases()] == [first.id]


def test_a_changed_setting_records_a_second_release(control):
    plane, _ = control
    seed_record(plane)
    before = plane.releases.prepare()

    # A setting no fixture fills in for an absent capability, so this is a real
    # change in every workspace.
    plane.dns.update_domain("example.com", DomainPatch(always_use_https=True), "alice")
    after = plane.releases.prepare()

    assert after.id != before.id
    assert {item.id for item in plane.releases.list_releases()} == {
        before.id,
        after.id,
    }


def test_a_release_can_be_read_back_by_its_short_id_or_its_full_digest(control):
    plane, _ = control
    seed_record(plane)
    release = plane.releases.prepare()

    assert plane.releases.get(release.id).digest == release.digest
    assert plane.releases.get(release.digest).digest == release.digest
    with pytest.raises(NotFoundError):
        plane.releases.get("0" * 16)


def test_the_inputs_a_release_was_compiled_from_survive_beside_it(control):
    """What a rollback restores. The artifact is not enough.

    Converging an old artifact would put the edges back and leave the control
    plane still asserting the newer state, so the next reconciliation would
    quietly undo the rollback. Adopting the inputs is what makes it a change of
    desired target.
    """
    plane, _ = control
    seed_record(plane)
    release = plane.releases.prepare()

    inputs = plane.releases.inputs_for(release.id)

    assert [zone.name for zone in inputs.domains] == ["example.com"]
    assert inputs.digest == release.inputs_digest


def test_pruning_keeps_a_release_a_deployment_still_names(control):
    """Age is no reason to drop the fleet's way back to a state."""
    plane, repository = control
    seed_record(plane)
    deployed = plane.deployments.deploy("alice")

    # Enough newer releases to push the deployed one well past any bound.
    for index in range(5):
        plane.dns.create_domain(Domain(name=f"zone-{index}.example.com"), "alice")
        plane.releases.prepare()
    repository.releases.prune(1)

    assert plane.releases.get(deployed.release_id).id == deployed.release_id


def test_a_deployment_converges_the_document_its_release_carries(control, settings):
    """The artifact on disk is the release's bytes, not a fresh derivation."""
    plane, _ = control
    seed_record(plane)

    deployment = plane.deployments.deploy("alice")
    release = plane.releases.get(deployment.release_id)

    published = settings.generated_vars_path.read_text(encoding="utf-8")
    for site in release.artifact(DESIRED_STATE_ARTIFACT).document[
        "blitzecdn_nginx_sites"
    ]:
        assert site["name"] in published


def test_a_second_deploy_of_unchanged_state_names_the_same_release(control):
    """ "Has anything changed" becomes a string comparison.

    It used to be a full serialised document per deployment row, with nothing
    saying two of them were the same.
    """
    plane, _ = control
    seed_record(plane)

    first = plane.deployments.deploy("alice")
    second = plane.deployments.deploy("alice")

    assert first.release_id == second.release_id


def test_a_release_a_capability_objected_to_carries_no_artifact(control):
    """A capability that objected to a site must not then be asked to render it.

    That is not squeamishness: the objection is usually the reason asking would
    fail. `blitzecdn-certificates` objects to a site in a controller-managed
    TLS mode whose material was never issued, and asking it for that site's
    certificate paths raises about exactly the material it just objected about
    — so an operator would meet the exception instead of the objection.
    """
    plane, _ = control
    seed_record(plane)

    def refuse(site):
        from blitzecdn.capabilities.releases.domain import ReleaseFinding

        return (ReleaseFinding(source="waf", host=site.name, message="rules unparsed"),)

    plane.releases.site_checks = refuse
    release = plane.releases.compile()

    assert not release.servable
    with pytest.raises(UnservableReleaseError, match="cannot be converged"):
        release.artifact(DESIRED_STATE_ARTIFACT)


def test_a_release_missing_a_capability_still_shows_what_would_be_sent(control):
    """The wheel is not there to be asked, so it contributes nothing.

    Nothing converges the release — `servable` says so — but an operator
    deciding what to install wants to see the document this installation would
    actually produce.
    """
    plane, _ = control
    plane.edges.add_edge(
        Edge(
            name="edge-a",
            host="192.0.2.10",
            ssh_sources=("198.51.100.0/24",),
            capabilities=("cache",),
        ),
        "alice",
    )
    # Compression named rather than left to the seed: it is the capability this
    # edge is declared not to have, and the seed would switch it off in a
    # workspace where the wheel is detached.
    seed_site(plane, compression="gzip")

    release = plane.releases.compile()

    assert not release.servable
    assert release.artifact(DESIRED_STATE_ARTIFACT).document["blitzecdn_nginx_sites"]


def test_a_release_explains_every_host_it_compiled(control):
    plane, _ = control
    seed_record(plane, name="cdn")

    (explanation,) = plane.releases.compile().explanations

    assert explanation.host == "example-com"
    assert explanation.server_names == ("cdn.example.com",)
    assert {item.setting for item in explanation.settings} >= {
        "cache_enabled",
        "ssl_mode",
    }


def test_the_edges_a_release_is_aimed_at_are_the_ones_a_limit_names(control):
    """A canary is validated against the edge it will reach and no other."""
    plane, _ = control
    for name in ("edge-a", "edge-b"):
        plane.edges.add_edge(
            Edge(
                name=name, host=f"192.0.2.{name[-1]}", ssh_sources=("198.51.100.0/24",)
            ),
            "alice",
        )

    assert {target.name for target in plane.releases.edge_capabilities()} == {
        "edge-a",
        "edge-b",
    }
    assert [
        target.name for target in plane.releases.edge_capabilities(host_limit="edge-a")
    ] == ["edge-a"]


def test_a_site_an_edge_cannot_serve_is_refused_before_the_run_starts(control):
    """The check the edge role can only make halfway through a converge.

    An nginx that never loaded the Brotli module reads `brotli on` as a syntax
    error, and by then the run is already touching hosts.
    """
    plane, _ = control
    plane.edges.add_edge(
        Edge(
            name="edge-a",
            host="192.0.2.10",
            ssh_sources=("198.51.100.0/24",),
            capabilities=("cache",),
        ),
        "alice",
    )
    seed_site(plane, compression="gzip")

    release = plane.releases.compile()

    assert not release.servable
    assert any(
        "edge 'edge-a' does not provide capability 'compression'" in finding.message
        for finding in release.findings
    )


def test_unproxying_the_last_hostname_leaves_a_release_with_no_sites(control):
    """Desired state a rollback must restore, and an instruction to no edge."""
    plane, _ = control
    seed_record(plane, name="cdn")
    plane.dns.unproxy("example.com", "cdn", RecordType.A, "203.0.113.9", "alice")

    release = plane.releases.compile()

    assert release.sites == ()
    assert (
        release.artifact(DESIRED_STATE_ARTIFACT).document["blitzecdn_nginx_sites"] == []
    )
