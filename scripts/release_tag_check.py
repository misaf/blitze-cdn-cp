"""Refuse a release tag that is not the signed, annotated tag CONTRIBUTING asks for.

    python3 scripts/release_tag_check.py

Run by every workflow that publishes from a `v*.*.*` tag, before it publishes.
It reads the tag from GitHub Actions' environment (`GITHUB_REF_NAME`,
`GITHUB_REPOSITORY`, `GITHUB_SHA`, `GITHUB_TOKEN`) and fails unless:

- the tag is `v` plus the version in `pyproject.toml`, so an image is never
  published under a version its own code does not report;
- the tag is annotated, not lightweight, and points at the commit being built;
- GitHub verifies the tag's signature;
- the tag message is `BlitzeCDN vX.Y.Z`, which `just release-tag` writes.

Standard library only, and no newer than the runner's system `python3`.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

Fetch = Callable[[str], Mapping[str, Any]]


def problems(
    tag: str,
    version: str,
    commit: str,
    fetch: Fetch,
) -> list[str]:
    """Everything wrong with `tag`, using `fetch` to read GitHub's git API."""
    found: list[str] = []
    if tag != f"v{version}":
        found.append(f"release tag {tag!r} must equal v{version}")

    ref = fetch(f"git/ref/tags/{tag}")
    if ref["object"]["type"] != "tag":
        found.append(
            f"{tag} is a lightweight tag; push an annotated, signed tag "
            "(just release-tag X.Y.Z)"
        )
        return found

    obj = fetch(f"git/tags/{ref['object']['sha']}")
    if obj["object"]["sha"] != commit:
        found.append(f"{tag} points at {obj['object']['sha']}, not {commit}")
    verification = obj.get("verification") or {}
    if not verification.get("verified"):
        reason = verification.get("reason", "unsigned")
        found.append(f"GitHub does not verify the signature on {tag} ({reason})")
    subject = str(obj.get("message", "")).split("\n", 1)[0].strip()
    if subject != f"BlitzeCDN v{version}":
        found.append(f"{tag}'s message is {subject!r}, not 'BlitzeCDN v{version}'")
    return found


def _github(repository: str, token: str, api: str) -> Fetch:
    # The token goes only to an HTTPS API; urllib would also open file: URLs.
    if not api.startswith("https://"):
        raise SystemExit(f"error: GITHUB_API_URL must be https, got {api!r}")

    def fetch(path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            f"{api}/repos/{repository}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - scheme checked above
            data: Mapping[str, Any] = json.load(response)
            return data

    return fetch


def main() -> int:
    env = os.environ
    project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    found = problems(
        tag=env["GITHUB_REF_NAME"],
        version=project["project"]["version"],
        commit=env["GITHUB_SHA"],
        fetch=_github(
            env["GITHUB_REPOSITORY"],
            env["GITHUB_TOKEN"],
            env.get("GITHUB_API_URL", "https://api.github.com"),
        ),
    )
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
