"""Nginx template fragments an installed capability contributes.

Answered by ``blitzecdn_nginx_contributions``, and rendered into each release's
configuration tree on the controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "NginxContribution",
]


@dataclass(frozen=True, slots=True)
class NginxContribution:
    """Static Nginx template fragments shipped by a feature.

    The four contexts are deliberately structural rather than directive-level:
    one global ``http`` resource, one server-level insertion point, one access
    phase before dispatch, and one upstream location after dispatch.  They are
    enough for current capabilities without allowing a package to replace a
    complete server block or turning Pluggy into a templating API.

    ``module_fragments`` are not Nginx configuration but executable njs
    modules, rendered into the runtime's modules directory under their name
    without ``.j2``. They ship in the configuration release, so they must not
    carry a secret.
    """

    feature: str
    templates_path: Path
    http_fragments: tuple[str, ...] = ()
    server_fragments: tuple[str, ...] = ()
    access_fragments: tuple[str, ...] = ()
    upstream_fragments: tuple[str, ...] = ()
    module_fragments: tuple[str, ...] = ()
