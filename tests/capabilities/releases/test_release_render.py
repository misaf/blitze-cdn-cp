"""`blitzecdn release render`: the edge configuration tree a release produces."""

from __future__ import annotations

import json
import os

import pytest
from control_plane_fixtures import cli_control_plane, seed_site
from paths import CORE_ANSIBLE
from typer.testing import CliRunner

from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.entrypoints.cli import main as cli

runner = CliRunner()


@pytest.fixture
def shipped(settings):
    """Settings rendering from the shipped Ansible tree, as an installation does."""
    return settings.model_copy(update={"ansible_dir": CORE_ANSIBLE})


def test_the_tree_is_written_as_an_edge_lays_it_out(shipped, monkeypatch, tmp_path):
    control = cli_control_plane(shipped, monkeypatch)
    seed_site(control)
    output = tmp_path / "tree"

    result = runner.invoke(
        cli.app, ["release", "render", "--output", str(output), "--json"]
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    release = control.releases.compile()
    bundle = control.releases.render(release)
    assert summary["release"] == release.id
    assert summary["bundle"] == bundle.digest
    for name, content in bundle.files.items():
        assert (output / name).read_bytes() == content
    for name, target in bundle.links.items():
        assert os.readlink(output / name) == target
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest == bundle.manifest()
    site = (output / "nginx/sites-available/example-com.conf").read_text()
    assert release.digest in site


def test_a_tree_is_never_written_over_another(shipped, monkeypatch, tmp_path):
    control = cli_control_plane(shipped, monkeypatch)
    seed_site(control)
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "keep.txt").write_text("mine")

    result = runner.invoke(cli.app, ["release", "render", "--output", str(output)])

    assert result.exit_code != 0
    assert "is not empty" in result.output
    assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]


def test_a_release_is_rendered_once_and_kept_as_rendered(shipped, monkeypatch):
    """A rollback ships what the release first rendered, whatever templates do now."""
    control = cli_control_plane(shipped, monkeypatch)
    seed_site(control)
    first = control.releases.prepare()
    kept = control.releases.configuration(first.id)

    calls = []
    real = control.releases.renderer

    class Changed:
        def render(self, document):
            calls.append(document)
            return ConfigurationBundle(files={"nginx.conf": b"changed\n"})

    monkeypatch.setattr(control.releases, "renderer", Changed())
    again = control.releases.prepare()

    assert again.id == first.id
    assert calls == []
    assert control.releases.configuration(first.id) == kept
    assert control.releases.render(first) == kept
    assert kept == real.render(first.execution_document())


def test_publishing_writes_the_bundle_before_the_document_that_names_it(
    shipped, monkeypatch, tmp_path
):
    import yaml

    from blitzecdn.capabilities.deployments.wiring import (
        CONFIGURATION_BUNDLE,
        publish_release,
    )

    control = cli_control_plane(shipped, monkeypatch)
    seed_site(control)
    release = control.releases.prepare()
    bundle = control.releases.configuration(release.id)
    variables = tmp_path / "state" / "desired-state.yml"

    publish_release(release, bundle, variables)

    document = yaml.safe_load(variables.read_text())
    written = variables.with_name(CONFIGURATION_BUNDLE)
    assert document["blitzecdn_config_release_bundle"] == str(written)
    assert document["blitzecdn_release_digest"] == release.digest
    assert ConfigurationBundle.decode(written.read_text()) == bundle
    # Where the controller keeps a file is not desired state.
    assert "blitzecdn_config_release_bundle" not in release.execution_document()
