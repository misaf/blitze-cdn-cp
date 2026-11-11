"""The shared secret that lets the controller read an edge's release marker.

`/.blitzecdn/release` answers the active release digest on every public
listener, because rollout verification has to observe it from outside, through
the very virtual host it is verifying. Served to anyone, it lets a stranger
fingerprint which configuration an edge runs and watch rollouts happen. The
edge therefore answers it only to a request carrying a signature made with this
token in :data:`RELEASE_PROBE_HEADER`, and 404 to everyone else, like every
other reserved path.

The token itself never crosses the network. Verification reaches a site over
plain HTTP when it serves no TLS, and over TLS without chain verification
otherwise, so anything sent is visible to whoever sits on that path. What is
sent instead is :func:`release_probe_signature`: Nginx's ``secure_link`` MD5
over an expiry, the hostname and the marker's path, keyed with the token. A
captured signature reads that one hostname's marker until it expires, minutes
later, and says nothing about the token.

The token is generated once per controller and kept under its state directory,
root-only. It reaches edges through the Ansible environment — never desired
state, a release or a command line — and is written into the edge's secrets
directory, outside every configuration release, so it changes no digest.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

__all__ = [
    "RELEASE_PROBE_ENVIRONMENT",
    "RELEASE_PROBE_FILE",
    "RELEASE_PROBE_HEADER",
    "RELEASE_PROBE_LIFETIME",
    "RELEASE_PROBE_PATH",
    "release_probe_signature",
    "release_probe_token",
]

#: The request header carrying :func:`release_probe_signature`, which the edge
#: checks with its copy of the token.
RELEASE_PROBE_HEADER = "X-BlitzeCDN-Probe"

#: Reserved by the edge configuration, never forwarded to a customer's origin.
RELEASE_PROBE_PATH = "/.blitzecdn/release"

#: Seconds a signature stays valid. Long enough to absorb the clock drift an
#: NTP-synchronised edge has, short enough that a captured one soon goes stale.
RELEASE_PROBE_LIFETIME = 300

#: How the token reaches the edge play; read by `blitzecdn_nginx`'s defaults.
RELEASE_PROBE_ENVIRONMENT = "BLITZE_RELEASE_PROBE_TOKEN"

#: Under the controller's state directory; backed up with the controller key.
RELEASE_PROBE_FILE = "release-probe.token"
_TOKEN = re.compile(r"[0-9a-f]{64}")


def release_probe_token(state_dir: Path) -> str:
    """This controller's token, created on first use.

    Created exclusively, so concurrent first callers (API, CLI, worker) agree
    on one value. A file that does not hold a well-formed token is an error
    rather than something to overwrite silently: every edge holds the old one.
    """
    path = state_dir / RELEASE_PROBE_FILE
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


def release_probe_signature(
    token: str, hostname: str, *, now: float | None = None
) -> str:
    """The :data:`RELEASE_PROBE_HEADER` value that reads ``hostname``'s marker.

    ``<hash>,<expires>`` as ``secure_link $http_x_blitzecdn_probe`` parses it,
    where the hash is the unpadded base64url MD5 of
    ``secure_link_md5 "$secure_link_expires$host$uri <token>"``. ``$host`` is
    lowercase without a trailing dot, so the hostname is normalised the same way.
    """
    expires = int(time.time() if now is None else now) + RELEASE_PROBE_LIFETIME
    host = hostname.lower().rstrip(".")
    material = f"{expires}{host}{RELEASE_PROBE_PATH} {token}".encode("ascii")
    digest = hashlib.md5(material, usedforsecurity=False).digest()
    return f"{base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')},{expires}"


def _create(path: Path) -> str:
    """Write a new token beside ``path``, then publish it with a hard link.

    The link is atomic and refuses an existing name, so a concurrent reader
    sees either no file or a complete one, and a competing creator's token is
    never replaced: the loser reads the winner's.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(32)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return path.read_text(encoding="ascii").strip()
    finally:
        os.unlink(temporary)
    return token
