"""The images BlitzeCDN builds start from bases that are pinned and tracked.

A tag is a pointer its publisher may move, so a Dockerfile naming only a tag builds a
different image on a different day, and a rebuilt base's security fixes reach the
fleet only when somebody happens to edit the tag. Each base is therefore pinned to the
digest of its multi-platform index — the reference both published architectures
resolve through — and Dependabot's `docker` ecosystem proposes the next digest as a
reviewable pull request instead.

The two halves need each other: a pin nobody updates goes stale, and an updater that
cannot read the `FROM` line updates nothing. Dependabot does not resolve
`FROM ${ARG}`, which is why these tests read the `FROM` lines themselves rather than
an `ARG` default.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from support.paths import REPO_ROOT

from blitzecdn.assets.docker import CONTROL_PLANE_DOCKERFILE, EDGE_DOCKERFILE

DEPENDABOT = REPO_ROOT / ".github/dependabot.yml"

_PINNED = re.compile(r"[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")
_FROM = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
_COPY_FROM_IMAGE = re.compile(r"^COPY\s+--from=(\S+:\S+)", re.MULTILINE)

DOCKERFILES = {
    "control-plane": CONTROL_PLANE_DOCKERFILE,
    "edge": EDGE_DOCKERFILE,
}


def _external_references(dockerfile: Path) -> list[str]:
    """Every image reference the file pulls from a registry.

    A `FROM` naming an earlier stage is not a pull, and `COPY --from=` a stage name
    neither; a `COPY --from=` with a tag in it is, and is held to the same rule.
    """
    text = dockerfile.read_text(encoding="utf-8")
    stages = set(
        re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", text, re.MULTILINE | re.IGNORECASE)
    )
    references = [ref for ref in _FROM.findall(text) if ref not in stages]
    references += _COPY_FROM_IMAGE.findall(text)
    return references


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_every_base_image_is_pinned_by_digest(name: str):
    references = _external_references(DOCKERFILES[name])

    assert references, f"{name}: no base image found"
    for reference in references:
        # `${...}` is refused by the same match: an image behind a build argument
        # is one Dependabot cannot see, whatever its default says.
        assert _PINNED.fullmatch(reference), (
            f"{name}: {reference} is not tag@sha256:digest"
        )


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_dependabot_tracks_every_dockerfile_directory(name: str):
    configuration = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    tracked = {
        directory
        for update in configuration["updates"]
        if update["package-ecosystem"] == "docker"
        for directory in [update.get("directory"), *update.get("directories", [])]
        if directory
    }
    directory = "/" + DOCKERFILES[name].parent.relative_to(REPO_ROOT).as_posix()

    assert directory in tracked, f"{directory} is not in a docker entry of {DEPENDABOT}"
