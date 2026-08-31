"""Translate structured edge reports into public origin-check results."""

from __future__ import annotations

from datetime import datetime

from blitzecdn.core.runs import HostRun
from blitzecdn.features.edges.origins import EdgeOriginChecks, OriginCheck


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
