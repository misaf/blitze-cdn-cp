"""`blitzecdn release render`: the edge configuration tree a release produces."""

from __future__ import annotations

import json
import os

import pytest
from control_plane_fixtures import cli_control_plane, seed_site
from paths import CORE_ANSIBLE
from typer.testing import CliRunner

from blitzecdn.cli import main as cli

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
