"""Nginx template fragments an installed capability contributes.

Answered by ``blitzecdn_nginx_contributions``, and rendered into the tree
``blitzecdn_nginx`` writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "NginxContribution",
]


@dataclass(frozen=True, slots=True)
class NginxContribution:
    """Static Nginx template fragments shipped by an installed package.

    The four contexts are deliberately structural rather than directive-level:
    one global ``http`` resource, one server-level insertion point, one access
    phase before dispatch, and one upstream location after dispatch.  They are
    enough for current capabilities without allowing a package to replace a
    complete server block or turning Pluggy into a templating API.
    """

    plugin: str
    templates_path: Path
    http_fragments: tuple[str, ...] = ()
    server_fragments: tuple[str, ...] = ()
    access_fragments: tuple[str, ...] = ()
    upstream_fragments: tuple[str, ...] = ()
