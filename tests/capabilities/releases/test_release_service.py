"""Recording a release, and what a deployment does with the one it named.

Against the real database: recording is idempotent by digest, pruning cannot
remove a release the fleet can be sent back to, and a deployment converges
its release's exact document.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from support.control_plane import FakeRunner, build_application, seed_record, seed_site

from blitzecdn.capabilities.dns.domain import (
    Domain,
    DomainPatch,
    PatchPolicy,
    RecordType,
)
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.releases.domain import (
    DESIRED_STATE_ARTIFACT,
    UnservableReleaseError,
)
from blitzecdn.composition import Repository
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.exceptions import NotFoundError


@pytest.fixture
def control(settings):
    repository = Repository(settings.database_path)
    return build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    ), repository


def test_compiling_records_nothing(control):
    """`blitzecdn validate` compiles on every invocation, so compiling records
    nothing.
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


def test_rendering_failure_keeps_admission_durable_and_retryable(
    control, settings, monkeypatch
):
    plane, repository = control
    seed_site(plane)
    renderer = plane.releases.renderer
    seen = []

    class InterruptedRenderer:
        def render(self, document):
            assert repository.database.current_session() is None
            # A separate database session sees admission already committed.
            other = Repository(settings.database_path)
            try:
                seen.extend(other.releases.list_releases())
            finally:
                other.close()
            raise RuntimeError("render interrupted")

    monkeypatch.setattr(plane.releases, "renderer", InterruptedRenderer())
    with pytest.raises(RuntimeError, match="render interrupted"):
        plane.releases.prepare()
    assert len(seen) == 1
    with pytest.raises(NotFoundError):
        plane.releases.configuration(seen[0].id)

    monkeypatch.setattr(plane.releases, "renderer", renderer)
    retried = plane.releases.prepare()
    assert retried.id == seen[0].id
    assert plane.releases.configuration(retried.id)


def test_admission_keeps_resource_resolution_and_retention_in_one_write_boundary(
    control, monkeypatch
):
    plane, repository = control
    seed_site(plane)
    observed = []
    contributions = plane.releases.contributors
    save = repository.releases.save

    def resolve(site):
        session = repository.database.current_session()
        assert session is not None
        observed.append(session)
        return contributions.site_variables(site)

    class ObservedContributions:
        def site_variables(self, site):
            return resolve(site)

        def fleet_variables(self, sites):
            return contributions.fleet_variables(sites)

    def retain(release, inputs):
        assert repository.database.current_session() is observed[0]
        return save(release, inputs)

    monkeypatch.setattr(plane.releases, "contributors", ObservedContributions())
    monkeypatch.setattr(repository.releases, "save", retain)
    plane.releases.prepare()
    assert observed


def test_a_changed_setting_records_a_second_release(control):
    plane, _ = control
    seed_record(plane)
    before = plane.releases.prepare()

    # A setting no fixture fills in for an absent capability, so this is a real
    # change in every workspace.
    plane.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(always_use_https=True)),
        "alice",
    )
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

    Converging an old artifact alone would leave the control plane asserting the
    newer state, so the next reconciliation would undo the rollback.
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
    envelope = yaml.safe_load(published)
    assert envelope.pop("blitzecdn_release_digest") == release.digest
    # The bundle the edge stages is the one stored with the release.
    bundle = Path(envelope.pop("blitzecdn_config_release_bundle"))
    assert ConfigurationBundle.decode(
        bundle.read_text()
    ) == plane.releases.configuration(release.id)
    assert envelope == release.artifact(DESIRED_STATE_ARTIFACT).document
    for site in release.artifact(DESIRED_STATE_ARTIFACT).document[
        "blitzecdn_nginx_sites"
    ]:
        assert site["name"] in published


def test_a_second_deploy_of_unchanged_state_names_the_same_release(control):
    """ "Has anything changed" becomes a string comparison."""
    plane, _ = control
    seed_record(plane)

    first = plane.deployments.deploy("alice")
    second = plane.deployments.deploy("alice")

    assert first.release_id == second.release_id


def test_a_release_a_capability_objected_to_carries_no_artifact(control):
    """A capability that objected to a site must not then be asked to render it.

    The objection is usually why rendering would fail: TLS objects to a managed
    mode with unissued material, and asking it for those paths would raise
    instead.
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
    """A missing capability contributes nothing, but the release still shows the
    document this installation would produce.
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
    # Compression named explicitly: it is the capability this edge declares it
    # lacks.
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

    An nginx without the Brotli module reads `brotli on` as a syntax error.
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
