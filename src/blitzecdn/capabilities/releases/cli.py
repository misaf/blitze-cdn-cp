"""Reading compiled releases from the command line.

Four verbs and no way to create one, for the same reason the routes are
read-only: a release is what canonical state compiles to, so the way to get a
different one is to change a zone, a rule or a record. ``blitzecdn release
show`` with no argument compiles the current state and records nothing, which
is the question an operator actually has before a deploy — "what am I about to
send, and why does this hostname look like that".
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.releases.domain import DESIRED_STATE_ARTIFACT, Release
from blitzecdn.cli import common

release_app = typer.Typer(help="Compiled desired state: what the fleet is served.")


def _summary(release: Release) -> dict[str, object]:
    """What a release is, flattened for a terminal.

    The digests are the point of it. ``id`` addresses the release, ``artifacts``
    says what each rendered document hashes to, and ``inputs_digest`` says which
    canonical state produced them — so "is the fleet serving what we think"
    is a comparison an operator can make by eye.
    """
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
def release_list(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
    json_output: common.JsonOutput = False,
) -> None:
    """Recorded releases, newest first."""
    common.emit(
        [
            _summary(item)
            for item in common.control_plane().releases.list_releases(limit)
        ],
        json_output=json_output,
    )


@release_app.command("show")
def release_show(
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
    releases = common.control_plane().releases
    release = (
        releases.get(release_id)
        if release_id is not None
        else releases.compile(host_limit=limit)
    )
    common.emit(_summary(release), json_output=json_output)


@release_app.command("explain")
def release_explain(
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
    releases = common.control_plane().releases
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
def release_artifact(
    release_id: Annotated[str | None, typer.Option("--release")] = None,
    name: Annotated[str, typer.Option("--name")] = DESIRED_STATE_ARTIFACT,
    json_output: common.JsonOutput = False,
) -> None:
    """The exact document the edges are converged from, and its digest."""
    releases = common.control_plane().releases
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
