#!/usr/bin/env python3
"""Read complete JSON log records; return bounded hourly deltas without writes.

Standalone standard-library code: Ansible copies this to an edge, where the
control-plane package is not installed. Offsets always refer to uncompressed
bytes. A stream's first record contains Nginx's generated request ID, so its
hash survives rename/compression without relying on reusable filesystem inodes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple, cast

MAX_LINE_BYTES = 65536
MAX_BATCHES = 128
MAX_COUNTER = 2**63 - 1
LOG_NAME = re.compile(r"^[a-z0-9_.-]+\.access\.log(?:\.\d+)?(?:\.gz)?$")
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
HOSTNAME_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
#: ``$upstream_cache_status`` values. Anything else, including an empty value
#: (the cache was never consulted), is counted as NONE: a cache label must never
#: block the traffic it rides on.
CACHE_OUTCOMES = frozenset(
    {"HIT", "STALE", "UPDATING", "REVALIDATED", "MISS", "EXPIRED", "BYPASS"}
)
#: Paths are counted per bucket in a bounded table. Past this many distinct
#: paths it keeps the busiest ``PATH_KEEP``, so counts are lower bounds and a
#: long tail of unique URLs cannot grow the collector's memory.
PATH_TRACK = 4096
PATH_KEEP = 1024
MAX_PATH = 512
MAX_TOP_PATHS = 100


class Record(NamedTuple):
    hostname: str
    hour: str
    bytes_sent: int
    body_bytes_sent: int
    outcome: str
    #: ``"2xx"`` and so on; None for a status outside 100-599 or none at all.
    status: str | None
    #: The request path without query or fragment; None when not a path.
    path: str | None


class CollectionError(ValueError):
    """A safe diagnostic containing no raw request data."""


def status_class(value: object) -> str | None:
    if type(value) is not int or not 100 <= value <= 599:
        return None
    return f"{value // 100}xx"


def request_path(value: object) -> str | None:
    """The path of ``METHOD PATH PROTOCOL``, never its query string.

    Query strings carry tokens and signatures, so they are cut here even when
    the edge logs them in full.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(" ")
    if len(parts) < 2 or not parts[1].startswith("/"):
        return None
    path = re.split(r"[?#]", parts[1], maxsplit=1)[0]
    return path[:MAX_PATH]


def usage(line: bytes) -> Record:
    record = json.loads(line)
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
    ):
        raise CollectionError("unsupported traffic record schema")
    if not isinstance(record.get("request_id"), str) or not REQUEST_ID.fullmatch(
        record["request_id"]
    ):
        raise CollectionError("missing or invalid generated request ID")
    host = record.get("hostname")
    if (
        not isinstance(host, str)
        or not 1 <= len(host) <= 253
        or not all(HOSTNAME_LABEL.fullmatch(label) for label in host.split("."))
    ):
        raise CollectionError("invalid traffic hostname")
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        raise CollectionError("missing traffic timestamp")
    date = datetime.fromisoformat(timestamp)
    if date.tzinfo is None:
        raise CollectionError("traffic timestamp requires a timezone")
    hour = date.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
    counters = [record.get(name) for name in ("bytes_sent", "body_bytes_sent")]
    if any(
        type(value) is not int or not 0 <= value <= MAX_COUNTER for value in counters
    ):
        raise CollectionError("invalid traffic byte counter")
    outcome = record.get("cache_status")
    if not isinstance(outcome, str) or outcome not in CACHE_OUTCOMES:
        outcome = "NONE"
    return Record(
        host,
        hour,
        cast(int, counters[0]),
        cast(int, counters[1]),
        outcome,
        status_class(record.get("status")),
        request_path(record.get("request")),
    )


def count_path(paths: dict[str, list[int]], path: str, sent: int) -> None:
    counters = paths.get(path)
    if counters is None:
        if len(paths) >= PATH_TRACK:
            busiest = sorted(paths.items(), key=lambda item: -item[1][0])[:PATH_KEEP]
            paths.clear()
            paths.update(busiest)
        counters = paths[path] = [0, 0]
    counters[0] += 1
    counters[1] += sent


def top_paths(paths: dict[str, list[int]], limit: int) -> list[dict[str, Any]]:
    busiest = sorted(paths.items(), key=lambda item: (-item[1][0], item[0]))
    return [
        {"path": path, "requests": requests, "bytes_sent": sent}
        for path, (requests, sent) in busiest[:limit]
    ]


def anchor(handle: BinaryIO, offset: int) -> str:
    start = max(0, offset - 64)
    handle.seek(start)
    tail = handle.read(offset - start)
    if len(tail) != offset - start:
        raise CollectionError("traffic log was truncated before its checkpoint")
    return hashlib.sha256(tail).hexdigest()


def add(
    buckets: dict[tuple[str, str], dict[str, Any]],
    paths: dict[tuple[str, str], dict[str, list[int]]],
    item: Record,
    path_limit: int,
) -> None:
    sent = item.bytes_sent
    bucket = buckets.setdefault(
        (item.hostname, item.hour),
        {
            "hostname": item.hostname,
            "hour": item.hour,
            "requests": 0,
            "bytes_sent": 0,
            "body_bytes_sent": 0,
            "cache": {},
            "status": {},
        },
    )
    bucket["requests"] += 1
    bucket["bytes_sent"] += sent
    bucket["body_bytes_sent"] += item.body_bytes_sent
    cache = bucket["cache"].setdefault(item.outcome, {"requests": 0, "bytes_sent": 0})
    cache["requests"] += 1
    cache["bytes_sent"] += sent
    if item.status is not None:
        bucket["status"][item.status] = bucket["status"].get(item.status, 0) + 1
    if path_limit and item.path is not None:
        count_path(paths.setdefault((item.hostname, item.hour), {}), item.path, sent)
    # Per-outcome counters are bounded by the bucket's own totals.
    if any(
        bucket[key] > MAX_COUNTER
        for key in ("requests", "bytes_sent", "body_bytes_sent")
    ):
        raise CollectionError("hourly traffic counter exceeds the integer range")


def read_stream(
    handle: BinaryIO,
    checkpoints: dict[str, dict[str, Any]],
    budget: int,
    seen: set[str],
    path_limit: int = 0,
) -> tuple[dict[str, Any] | None, int, bool]:
    first = handle.readline(MAX_LINE_BYTES + 1)
    if not first:
        return None, 0, False
    if len(first) > MAX_LINE_BYTES:
        raise CollectionError("traffic record exceeds the line limit")
    if not first.endswith(b"\n"):
        return None, 0, True
    usage(first)
    stream_id = hashlib.sha256(first).hexdigest()
    if stream_id in seen:
        return None, 0, False
    seen.add(stream_id)
    previous = checkpoints.get(stream_id)
    offset = previous["offset"] if previous else 0
    if previous and anchor(handle, offset) != previous["anchor"]:
        raise CollectionError("traffic log changed before its checkpoint")
    handle.seek(offset)
    consumed = 0
    more = False
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    paths: dict[tuple[str, str], dict[str, list[int]]] = {}
    while True:
        line = handle.readline(MAX_LINE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_LINE_BYTES:
            raise CollectionError("traffic record exceeds the line limit")
        if not line.endswith(b"\n") or consumed + len(line) > budget:
            more = True
            break
        add(buckets, paths, usage(line), path_limit)
        consumed += len(line)
    if not consumed:
        return None, 0, more
    for key, bucket in buckets.items():
        bucket["paths"] = top_paths(paths.get(key, {}), path_limit)
    end = offset + consumed
    return (
        {
            "start": previous,
            "end": {
                "stream_id": stream_id,
                "offset": end,
                "anchor": anchor(handle, end),
            },
            "buckets": list(buckets.values()),
        },
        consumed,
        more,
    )


def collect(document: dict[str, Any]) -> dict[str, Any]:
    directory = Path(document["log_directory"])
    budget = document.get("max_bytes", 256 * 1024 * 1024)
    if type(budget) is not int or not MAX_LINE_BYTES <= budget <= 1024 * 1024 * 1024:
        raise CollectionError("max_bytes must be between 65536 and 1073741824")
    path_limit = document.get("top_paths", 0)
    if type(path_limit) is not int or not 0 <= path_limit <= MAX_TOP_PATHS:
        raise CollectionError("top_paths must be between 0 and 100")
    checkpoints = {
        cursor["stream_id"]: cursor for cursor in document.get("checkpoints", [])
    }
    result: dict[str, Any] = {"batches": [], "errors": [], "more": False}
    if not directory.is_dir():
        result["errors"].append("hostname log directory is missing")
        return result
    # Old files first so an append-heavy current file cannot starve archives.
    paths = sorted(
        directory.iterdir(), key=lambda path: (path.name.endswith(".log"), path.name)
    )
    seen: set[str] = set()
    for path in paths:
        if not LOG_NAME.fullmatch(path.name):
            continue
        if budget <= 0 or len(result["batches"]) >= MAX_BATCHES:
            result["more"] = True
            break
        try:
            if path.is_symlink():
                raise CollectionError("refusing a symlink in hostname logs")
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rb") as handle:
                batch, consumed, more = read_stream(
                    cast(BinaryIO, handle), checkpoints, budget, seen, path_limit
                )
            result["more"] = result["more"] or more
            if batch:
                result["batches"].append(batch)
                budget -= consumed
        except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:
            # No raw log lines: request URLs and headers may contain secrets.
            message = (
                str(exc)
                if isinstance(exc, CollectionError)
                else "unreadable or invalid traffic log; retry after checking the file"
            )
            result["errors"].append(f"{path.name}: {message}")
    if not result["errors"] and not result["more"]:
        result["missing_streams"] = [
            stream_id for stream_id in checkpoints if stream_id not in seen
        ][:MAX_BATCHES]
    return result


def main() -> None:
    json.dump(collect(json.load(sys.stdin)), sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
