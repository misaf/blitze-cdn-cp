"""The shared secret that lets the controller read an edge's release marker.

`/.blitzecdn/release` answers the active release digest on every public
listener, because rollout verification has to observe it from outside, through
the very virtual host it is verifying. Served to anyone, it lets a stranger
fingerprint which configuration an edge runs and watch rollouts happen. The
edge therefore answers it only to a request carrying this token in
:data:`RELEASE_PROBE_HEADER`, and 404 to everyone else, like every other
reserved path.

The token is generated once per controller and kept under its state directory,
root-only. It reaches edges through the Ansible environment — never desired
state, a release or a command line — and is written into the edge's secrets
directory, outside every configuration release, so it changes no digest.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

__all__ = [
    "RELEASE_PROBE_ENVIRONMENT",
    "RELEASE_PROBE_HEADER",
    "release_probe_token",
]

#: The request header the edge compares against its copy of the token.
RELEASE_PROBE_HEADER = "X-BlitzeCDN-Probe"

#: How the token reaches the edge play; read by `blitzecdn_nginx`'s defaults.
RELEASE_PROBE_ENVIRONMENT = "BLITZE_RELEASE_PROBE_TOKEN"

_FILE_NAME = "release-probe.token"
_TOKEN = re.compile(r"[0-9a-f]{64}")


def release_probe_token(state_dir: Path) -> str:
    """This controller's token, created on first use.

    Created exclusively, so concurrent first callers (API, CLI, worker) agree
    on one value. A file that does not hold a well-formed token is an error
    rather than something to overwrite silently: every edge holds the old one.
    """
    path = state_dir / _FILE_NAME
    try:
        token = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        token = _create(path)
    if not _TOKEN.fullmatch(token):
        raise RuntimeError(
            f"{path} does not hold a release probe token; remove it to have a "
            "new one generated, then redeploy every edge"
        )
    return token


def _create(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="ascii").strip()
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(token + "\n")
    return token
