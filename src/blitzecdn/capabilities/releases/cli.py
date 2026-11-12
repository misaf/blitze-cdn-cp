"""Reading compiled releases from the command line.

Read-only like the routes. ``release show`` with no argument compiles current
state and records nothing: "what am I about to send".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from dishka import FromDishka

from blitzecdn.capabilities.releases.domain import DESIRED_STATE_ARTIFACT, Release
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.delivery.cli import common

release_app = typer.Typer(help="Compiled desired state: what the fleet is served.")


def _summary(release: Release) -> dict[str, object]:
    """What a release is, flattened for a terminal, digests first."""
    return {
        "id": release.id,
        "digest": release.digest,
        "compiler_version": release.compiler_version,
        "inputs_digest": release.inputs_digest,
        "capabilities": list(release.capabilities),
        "targets": [target.name for target in release.targets],
        "sites": [entry.name for entry in release.sites],
        "artifacts": dict(release.digests()),
        "servable": release.servable,
        "findings": [finding.message for finding in release.findings],
    }


@release_app.command("list")
@common.inject
def release_list(
    releases: FromDishka[ReleaseService],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    json_output: common.JsonOutput = False,
) -> None:
    """Recorded releases, newest first."""
    common.emit(
        [_summary(item) for item in releases.list_releases(limit)],
        json_output=json_output,
    )


@release_app.command("show")
@common.inject
def release_show(
    releases: FromDishka[ReleaseService],
    release_id: Annotated[
        str | None,
        typer.Argument(help="A recorded release id; omitted, compiles current state."),
    ] = None,
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """What a release is, or what current desired state would compile to.

    Compiling records nothing, so this is safe to run as often as it takes to
    get a configuration right.
    """
    release = (
        releases.get(release_id)
        if release_id is not None
        else releases.compile(host_limit=limit)
    )
    common.emit(_summary(release), json_output=json_output)


@release_app.command("explain")
@common.inject
def release_explain(
    releases: FromDishka[ReleaseService],
    host: Annotated[
        str | None,
        typer.Option("--host", help="Only this derived host, e.g. 'example-com--api'."),
    ] = None,
    release_id: Annotated[str | None, typer.Option("--release")] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Where each effective setting came from: the rule, the zone, or the default.

    The half ``domain resolve`` could not answer. Knowing that rule 'api' won
    does not say which values it actually chose, and a rule that wins while
    overriding nothing relevant leaves the zone's value in place — so an
    operator told only the rule's name goes and edits the wrong object.
    """
    release = releases.get(release_id) if release_id else releases.compile()
    explanations = [
        {
            "host": item.host,
            "zone": item.zone,
            "rule": item.rule,
            "server_names": list(item.server_names),
            "origin_host": item.origin_host,
            "settings": [
                {
                    "setting": setting.setting,
                    "value": setting.value,
                    "origin": setting.origin.value,
                    "rule": setting.rule,
                }
                for setting in item.settings
            ],
        }
        for item in release.explanations
        if host is None or item.host == host
    ]
    common.emit(explanations, json_output=json_output)


@release_app.command("artifact")
@common.inject
def release_artifact(
    releases: FromDishka[ReleaseService],
    release_id: Annotated[str | None, typer.Option("--release")] = None,
    name: Annotated[str, typer.Option("--name")] = DESIRED_STATE_ARTIFACT,
    json_output: common.JsonOutput = False,
) -> None:
    """The exact document the edges are converged from, and its digest."""
    release = releases.get(release_id) if release_id else releases.compile()
    artifact = release.artifact(name)
    common.emit(
        {
            "release": release.id,
            "name": artifact.name,
            "digest": artifact.digest,
            "document": artifact.document,
        },
        json_output=json_output,
    )


@release_app.command("diff")
@common.inject
def release_diff(
    releases: FromDishka[ReleaseService],
    json_output: common.JsonOutput = False,
) -> None:
    """What deploying now would change on the edges, as a unified diff.

    Current desired state, rendered, against one baseline: the release of the
    newest run that reached any edge. When the edges are recorded on different
    releases, or a run left one unverified, it says so first and names them.
    Per-edge releases come from deployment records, not live probes. Records
    nothing, so run it after every edit; secrets are never in the diff.
    """
    diff = releases.diff()
    if json_output:
        common.emit(diff.summary(), json_output=True)
        return
    fleet = diff.fleet
    for warning in fleet.warnings():
        typer.echo(f"Warning: {warning}.", err=True)
    if fleet.mixed:
        typer.echo("Recorded releases (from deployment records):", err=True)
        for group in fleet.groups:
            label = group.release or "no verified release"
            marks = ["baseline"] if group.baseline else []
            if group.changed is not None:
                marks.append("would change" if group.changed else "unchanged")
            suffix = f" ({', '.join(marks)})" if marks else ""
            typer.echo(f"  {label}{suffix}: {', '.join(group.edges)}", err=True)
    configuration = diff.configuration
    baseline = diff.baseline or "nothing deployed"
    if not configuration.changed:
        typer.echo(
            f"No changes against baseline {baseline}: "
            f"release {diff.release.id} renders the same."
        )
        return
    typer.echo(f"Baseline: {baseline}; desired: {diff.release.id}.", err=True)
    typer.echo(configuration.patch(), nl=False)
    for change in configuration.changes:
        if change.diff is None:
            typer.echo(f"Binary {change.change.value}: {change.path}")


@release_app.command("render")
@common.inject
def release_render(
    releases: FromDishka[ReleaseService],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="An empty or missing directory.")
    ],
    release_id: Annotated[str | None, typer.Option("--release")] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Write the edge configuration tree a release renders to, as an edge would.

    The tree is the candidate an edge serves from, links included, with a
    `manifest.json` in the format the edge seals it with — so what reaches an
    edge can be read, diffed and reviewed before a deploy.
    """
    if output.exists() and any(output.iterdir()):
        raise typer.BadParameter(f"{output} is not empty", param_hint="--output")
    release = releases.get(release_id) if release_id else releases.compile()
    bundle = releases.render(release)
    for name, content in bundle.files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for name, target in bundle.links.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    (output / "manifest.json").write_text(
        json.dumps(bundle.manifest(), sort_keys=True), encoding="utf-8"
    )
    common.emit(
        {
            "release": release.id,
            "bundle": bundle.digest,
            "files": len(bundle.files),
            "links": len(bundle.links),
            "output": str(output),
        },
        json_output=json_output,
    )
