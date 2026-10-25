"""Read the origin report one edge published into this capability's values.

Domain rather than adapter: it is a pure function from a recorded host run to
`EdgeOriginChecks`, with no I/O. One copy: the origin check and the Automatic
SSL/TLS scan both read the report through it, so a change to the row the role
publishes is made once.

Deliberately defensive at every step. The document comes off an edge's
``blitzecdn_report`` fact, which is Ansible's rendering of a role's variables:
a row that does not parse is dropped rather than failing the whole fleet's
answer, because a partial origin report is the useful one and an exception here
would discard four healthy edges over one malformed row.
"""

from __future__ import annotations

from datetime import datetime

from blitzecdn.capabilities.edges.domain.origins import OriginCheck
from blitzecdn.capabilities.origins.domain.report import EdgeOriginChecks
from blitzecdn.core.domain.runs import HostRun


def edge_origins(host: HostRun) -> EdgeOriginChecks:
    """Read one edge's published origin report defensively."""
    if not host.reached:
        return EdgeOriginChecks(host=host.host, error="unreachable")
    if not host.succeeded:
        return EdgeOriginChecks(
            host=host.host, error="the origin check role failed on this edge"
        )
    document = host.report
    if document is None:
        return EdgeOriginChecks(host=host.host, error="the edge published no report")
    rows = document.get("origins")
    checks: list[OriginCheck] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            checks.append(OriginCheck.model_validate(_origin_row(row)))
        except ValueError:
            continue
    collected = document.get("collected_at")
    try:
        checked_at = datetime.fromisoformat(str(collected)) if collected else None
    except ValueError:
        checked_at = None
    return EdgeOriginChecks(host=host.host, checked_at=checked_at, checks=tuple(checks))


def _origin_row(row: dict[str, object]) -> dict[str, object]:
    status = _as_int(row.get("status"))
    tls = row.get("tls_verified")
    detail = str(row.get("detail") or "").strip()
    return {
        "site": row.get("site"),
        "origin": row.get("origin"),
        "scheme": row.get("scheme"),
        "ssl_mode": row.get("ssl_mode"),
        "sni": row.get("sni") or None,
        "reachable": _as_bool(row.get("reachable")),
        "tls_verified": None if tls in (None, "", "None") else _as_bool(tls),
        "status": status if status and status > 0 else None,
        "content_sha256": row.get("content_sha256") or None,
        "detail": detail or None,
    }


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["edge_origins"]
