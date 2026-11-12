"""What deploying now would change on the edges, and what a deployment changed."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from support.control_plane import (
    API_HEADERS,
    cli_control_plane,
    control_plane_app,
    seed_edge,
    seed_site,
    serve,
)
from typer.testing import CliRunner

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.core.domain.configuration import ConfigurationChangeKind
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

    assert diff.live is None
    assert diff.configuration.before is None
    bundle = control.releases.render(diff.release)
    assert _changes(diff) == dict.fromkeys(
        [*bundle.files, *bundle.links], ConfigurationChangeKind.ADDED
    )


def test_an_edit_shows_as_a_diff_against_what_the_fleet_runs(control):
    deployed = control.deployments.deploy("alice")
    assert deployed.status is DeploymentStatus.SUCCEEDED

    unchanged = control.releases.diff()
    assert unchanged.live == deployed.release_id
    assert unchanged.release.id == deployed.release_id
    assert not unchanged.configuration.changed

    _edit(control, always_use_https=True)
    edited = control.releases.diff()

    assert edited.live == deployed.release_id
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
    assert current.json()["live_release"] == deployed.release_id
    assert past.status_code == 200, past.text
    assert past.json() == json.loads(
        runner.invoke(cli.app, ["deployment", "diff", deployed.id, "--json"]).output
    )
    assert record.json()["previous_release_id"] == deployed.previous_release_id
