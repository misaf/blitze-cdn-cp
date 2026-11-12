"""What deploying now would change on the edges, and what a deployment changed."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from support.control_plane import (
    API_HEADERS,
    FakeRunner,
    ansible_run,
    cli_control_plane,
    control_plane_app,
    host_run,
    seed_edge,
    seed_site,
    serve,
)
from typer.testing import CliRunner

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.releases import EdgeReleaseState
from blitzecdn.core.domain.configuration import ConfigurationChangeKind
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.entrypoints.cli import main as cli

runner = CliRunner()

SITE = "nginx/sites-available/example-com.conf"


@pytest.fixture
def control(settings, monkeypatch):
    control = cli_control_plane(settings, monkeypatch)
    seed_edge(control, name="edge-a")
    seed_site(control)
    yield control
    control.container.close()


def _edit(control, **policy):
    """Change the zone's policy, leaving its proxied hostname as it is."""
    seed_site(control, routed=False, **policy)


def _changes(diff):
    return {change.path: change.change for change in diff.configuration.changes}


def test_before_any_deployment_every_rendered_file_is_new(control):
    diff = control.releases.diff()

    assert diff.baseline is None
    assert diff.configuration.before is None
    bundle = control.releases.render(diff.release)
    assert _changes(diff) == dict.fromkeys(
        [*bundle.files, *bundle.links], ConfigurationChangeKind.ADDED
    )


def test_an_edit_shows_as_a_diff_against_what_the_fleet_runs(control):
    deployed = control.deployments.deploy("alice")
    assert deployed.status is DeploymentStatus.SUCCEEDED

    unchanged = control.releases.diff()
    assert unchanged.baseline == deployed.release_id
    assert unchanged.release.id == deployed.release_id
    assert not unchanged.configuration.changed

    _edit(control, always_use_https=True)
    edited = control.releases.diff()

    assert edited.baseline == deployed.release_id
    assert edited.release.id != deployed.release_id
    assert _changes(edited)[SITE] is ConfigurationChangeKind.MODIFIED
    assert f"--- a/{SITE}\n+++ b/{SITE}\n" in edited.configuration.patch()
    # Diffing records nothing: the edit's release is not on file.
    assert edited.release.id not in [r.id for r in control.releases.list_releases()]


def test_a_deployment_records_what_it_changed(control):
    first = control.deployments.deploy("alice")
    _edit(control, always_use_https=True)
    second = control.deployments.deploy("alice")

    assert first.previous_release_id is None
    assert second.previous_release_id == first.release_id
    assert not control.deployments.diff(first.id).before
    changed = control.deployments.diff(second.id)
    assert {c.path: c.change for c in changed.changes}[SITE] is (
        ConfigurationChangeKind.MODIFIED
    )
    # Release pruning must keep what a recorded deployment was queued over.
    assert first.release_id in control.repository.deployments.in_use()


def test_the_cli_prints_a_unified_diff_or_says_nothing_changed(control):
    control.deployments.deploy("alice")

    quiet = runner.invoke(cli.app, ["release", "diff"])
    assert quiet.exit_code == 0, quiet.output
    assert quiet.output.startswith("No changes")

    _edit(control, always_use_https=True)
    loud = runner.invoke(cli.app, ["release", "diff"])
    assert loud.exit_code == 0, loud.output
    assert f"+++ b/{SITE}" in loud.output

    document = json.loads(runner.invoke(cli.app, ["release", "diff", "--json"]).output)
    assert document["changed"] is True
    assert SITE in [change["path"] for change in document["changes"]]


def test_the_cli_shows_a_past_deployments_diff(control):
    control.deployments.deploy("alice")
    _edit(control, always_use_https=True)
    second = control.deployments.deploy("alice")

    result = runner.invoke(cli.app, ["deployment", "diff", second.id, "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["deployment_id"] == second.id
    assert document["previous_release_id"] == second.previous_release_id
    assert SITE in [change["path"] for change in document["changes"]]


def test_api_and_cli_answer_alike(control, settings, monkeypatch):
    control.deployments.deploy("alice")
    _edit(control, always_use_https=True)
    deployed = control.deployments.deploy("alice")
    _edit(control, always_use_https=False)
    serve(monkeypatch, control)

    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/releases/current/diff").status_code == 401
        current = client.get("/v3/releases/current/diff", headers=API_HEADERS)
        past = client.get(f"/v3/deployments/{deployed.id}/diff", headers=API_HEADERS)
        record = client.get(f"/v3/deployments/{deployed.id}", headers=API_HEADERS)

    assert current.status_code == 200, current.text
    assert current.json() == json.loads(
        runner.invoke(cli.app, ["release", "diff", "--json"]).output
    )
    assert current.json()["baseline_release"] == deployed.release_id
    assert past.status_code == 200, past.text
    assert past.json() == json.loads(
        runner.invoke(cli.app, ["deployment", "diff", deployed.id, "--json"]).output
    )
    assert record.json()["previous_release_id"] == deployed.previous_release_id


class _EdgeFails(FakeRunner):
    """One edge fails the phase run under ``tags`` while ``failing`` is set."""

    def __init__(self, edge: str, tags: tuple[str, ...]) -> None:
        super().__init__()
        self.edge = edge
        self.tags = tags
        self.failing = False

    def run(self, *, check, host_limit=None, tags=()):
        result = super().run(check=check, host_limit=host_limit, tags=tags)
        if self.failing and host_limit == self.edge and tags == self.tags:
            return ansible_run(
                host_run(self.edge, failure="boom"),
                status=RunStatus.FAILED,
                return_code=2,
            )
        return result


def _fleet(settings, monkeypatch, *edges, runner_double=None):
    control = cli_control_plane(settings, monkeypatch, runner_double=runner_double)
    for index, name in enumerate(edges):
        seed_edge(control, name=name, host=f"192.0.2.{10 + index}")
    seed_site(control)
    return control


def _placed(diff):
    return {edge.edge: (edge.state, edge.release) for edge in diff.fleet.edges}


def _groups(diff):
    return {
        group.release: (group.edges, group.baseline, group.changed)
        for group in diff.fleet.groups
    }


def test_a_uniform_fleet_is_not_mixed_and_carries_no_warning(control):
    deployed = control.deployments.deploy("alice")

    diff = control.releases.diff()

    assert _placed(diff) == {"edge-a": (EdgeReleaseState.RECORDED, deployed.release_id)}
    assert not diff.fleet.mixed
    assert diff.fleet.warnings() == ()
    assert diff.summary()["fleet"]["source"] == "deployment_records"


def test_a_canary_leaves_the_untouched_edges_named_behind_the_baseline(
    settings, monkeypatch
):
    control = _fleet(settings, monkeypatch, "edge-a", "edge-b", "edge-c")
    try:
        first = control.deployments.deploy("alice")
        _edit(control, always_use_https=True)
        canary = control.deployments.deploy("alice", host_limit="edge-a")
        assert canary.status is DeploymentStatus.SUCCEEDED

        diff = control.releases.diff()

        # The baseline is the canary's release, the newest to reach an edge...
        assert diff.baseline == canary.release_id
        # ...but two edges still run the first release.
        assert _placed(diff) == {
            "edge-a": (EdgeReleaseState.RECORDED, canary.release_id),
            "edge-b": (EdgeReleaseState.RECORDED, first.release_id),
            "edge-c": (EdgeReleaseState.RECORDED, first.release_id),
        }
        groups = _groups(diff)
        assert list(groups) == [canary.release_id, first.release_id]
        assert groups[canary.release_id][:2] == (("edge-a",), True)
        assert groups[first.release_id] == (("edge-b", "edge-c"), False, True)
        assert diff.fleet.mixed
        assert any("edge-b, edge-c" in note for note in diff.fleet.warnings())

        shown = runner.invoke(cli.app, ["release", "diff"])
        assert shown.exit_code == 0, shown.output
        assert "the fleet is mixed" in shown.output
        assert f"{first.release_id} (would change): edge-b, edge-c" in shown.output
        assert f"Baseline: {canary.release_id}" in shown.output
    finally:
        control.container.close()


def test_a_rollout_that_fails_after_the_canary_committed_is_mixed(
    settings, monkeypatch
):
    failing = _EdgeFails("edge-b", ("stage",))
    control = _fleet(settings, monkeypatch, "edge-a", "edge-b", runner_double=failing)
    try:
        first = control.deployments.deploy("alice")
        _edit(control, always_use_https=True)
        failing.failing = True
        partial = control.deployments.deploy("alice")
        failing.failing = False
        assert partial.status is DeploymentStatus.FAILED

        diff = control.releases.diff()

        assert diff.baseline == partial.release_id
        # Staging never changes what an edge serves, so edge-b is still on record.
        assert _placed(diff) == {
            "edge-a": (EdgeReleaseState.RECORDED, partial.release_id),
            "edge-b": (EdgeReleaseState.RECORDED, first.release_id),
        }
        assert diff.fleet.mixed
        assert _groups(diff)[first.release_id] == (("edge-b",), False, True)
    finally:
        control.container.close()


def test_a_failure_after_activation_leaves_the_edge_unverified(settings, monkeypatch):
    failing = _EdgeFails("edge-b", ("activate",))
    control = _fleet(settings, monkeypatch, "edge-a", "edge-b", runner_double=failing)
    try:
        first = control.deployments.deploy("alice")
        _edit(control, always_use_https=True)
        failing.failing = True
        partial = control.deployments.deploy("alice")
        failing.failing = False
        assert partial.status is DeploymentStatus.FAILED

        diff = control.releases.diff()

        # Withdrawn, but not verified since: its last verified release is kept.
        assert _placed(diff)["edge-b"] == (
            EdgeReleaseState.UNVERIFIED,
            first.release_id,
        )
        assert any("unverified: edge-b" in note for note in diff.fleet.warnings())
    finally:
        control.container.close()


def test_edges_never_deployed_are_unknown(settings, monkeypatch):
    control = _fleet(settings, monkeypatch, "edge-a", "edge-b")
    try:
        control.deployments.deploy("alice", host_limit="edge-a")
        seed_edge(control, name="edge-c", host="192.0.2.20")

        diff = control.releases.diff()

        assert _placed(diff)["edge-b"] == (EdgeReleaseState.UNKNOWN, None)
        assert _placed(diff)["edge-c"] == (EdgeReleaseState.UNKNOWN, None)
        assert _groups(diff)[None] == (("edge-b", "edge-c"), False, None)
        assert any(
            "no verified release on record: edge-b, edge-c" in note
            for note in diff.fleet.warnings()
        )
    finally:
        control.container.close()


def test_an_empty_fleet_says_so(settings, monkeypatch):
    control = cli_control_plane(settings, monkeypatch)
    try:
        seed_site(control)

        document = control.releases.diff().summary()

        assert document["baseline_release"] is None
        assert document["fleet"]["edges"] == []
        assert document["fleet"]["mixed"] is False
        assert document["fleet"]["warnings"] == [
            "no edges are registered; the baseline comes from deployment history alone"
        ]
    finally:
        control.container.close()


def test_the_fleet_view_leaves_past_deployment_diffs_and_retention_alone(
    settings, monkeypatch
):
    control = _fleet(settings, monkeypatch, "edge-a", "edge-b")
    try:
        first = control.deployments.deploy("alice")
        _edit(control, always_use_https=True)
        canary = control.deployments.deploy("alice", host_limit="edge-a")

        control.releases.diff()

        assert canary.previous_release_id == first.release_id
        assert control.deployments.diff(canary.id).changed
        in_use = control.repository.deployments.in_use()
        assert {first.release_id, canary.release_id} <= set(in_use)
    finally:
        control.container.close()
