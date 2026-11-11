"""The edge's release probe check, for test doubles standing in for an edge.

Written from the Nginx ``secure_link`` documentation rather than by calling
:func:`blitzecdn.core.ansible.probe.release_probe_signature`, so a double built
on it disagrees with a signature the edge would also refuse.
"""

from __future__ import annotations

import base64
import hashlib
import time


def edge_accepts(header: str | None, *, token: str, host: str, uri: str) -> bool:
    """Whether ``secure_link`` would set ``$secure_link`` to ``"1"``.

    ``secure_link_md5 "$secure_link_expires$host$uri <token>"``, as the
    ``blitzecdn_nginx`` role installs it.
    """
    if not header or "," not in header:
        return False
    received, expires = header.split(",", 1)
    if not expires.isdigit() or int(expires) <= time.time():
        return False
    material = f"{expires}{host.lower().rstrip('.')}{uri} {token}".encode()
    expected = base64.b64encode(
        hashlib.md5(material, usedforsecurity=False).digest(), altchars=b"-_"
    )
    return received == expected.decode().rstrip("=")
