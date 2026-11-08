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
#: Paths returned per hostname and hour, ranked by requests.
TOP_PATHS = 10
#: Distinct paths counted per hostname and hour; later new paths are not
#: tracked, so memory stays bounded whatever the URL cardinality.
MAX_TRACKED_PATHS = 1000
MAX_PATH_LENGTH = 512
PRINTABLE_PATH = re.compile(r"^/[\x21-\x7e]*$")


class CollectionError(ValueError):
    """A safe diagnostic containing no raw request data."""


class Record(NamedTuple):
    host: str
    hour: str
    bytes_sent: int
    body_bytes_sent: int
    outcome: str
    #: "2xx" and so on; None for a value outside 100-599.
    status: str | None
    #: The request path without query or fragment; None when not countable.
    path: str | None


def status_class(value: object) -> str | None:
    """Never blocks collection: an unexpected status is simply not classed."""
    if type(value) is not int or not 100 <= value <= 599:
        return None
    return f"{value // 100}xx"


def request_path(value: object) -> str | None:
    """The path of ``METHOD PATH PROTOCOL``, stripped of query and fragment.

    Query strings may carry credentials, so they never leave the edge even
    when the log keeps them. Anything unusual is skipped, not guessed at.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(" ")
    if len(parts) != 3:
        return None
    path = parts[1].split("?", 1)[0].split("#", 1)[0]
    if len(path) > MAX_PATH_LENGTH or not PRINTABLE_PATH.fullmatch(path):
        return None
    return path


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


def count(buckets: dict[tuple[str, str], dict[str, Any]], record: Record) -> None:
    """Add one record to its hostname and hour."""
    bucket = buckets.setdefault(
        (record.host, record.hour),
        {
            "hostname": record.host,
            "hour": record.hour,
            "requests": 0,
            "bytes_sent": 0,
            "body_bytes_sent": 0,
            "cache": {},
            "status": {},
        },
    )
    bucket["requests"] += 1
    bucket["bytes_sent"] += record.bytes_sent
    bucket["body_bytes_sent"] += record.body_bytes_sent
    cache = bucket["cache"].setdefault(record.outcome, {"requests": 0, "bytes_sent": 0})
    cache["requests"] += 1
    cache["bytes_sent"] += record.bytes_sent
    if record.status is not None:
        bucket["status"][record.status] = bucket["status"].get(record.status, 0) + 1
    # Per-outcome, status and path counters are bounded by the bucket's totals.
    if any(
        bucket[key] > MAX_COUNTER
        for key in ("requests", "bytes_sent", "body_bytes_sent")
    ):
        raise CollectionError("hourly traffic counter exceeds the integer range")


def track_path(tracked: dict[str, list[int]], record: Record) -> None:
    """Count a path, unless it would be one more than the bounded set holds."""
    if record.path is None:
        return
    counted = tracked.get(record.path)
    if counted is None:
        if len(tracked) >= MAX_TRACKED_PATHS:
            return
        counted = tracked[record.path] = [0, 0]
    counted[0] += 1
    counted[1] += record.bytes_sent


def top_paths(tracked: dict[str, list[int]]) -> list[dict[str, Any]]:
    ranked = sorted(tracked.items(), key=lambda item: (-item[1][0], item[0]))
    return [
        {"path": path, "requests": counted[0], "bytes_sent": counted[1]}
        for path, counted in ranked[:TOP_PATHS]
    ]


def anchor(handle: BinaryIO, offset: int) -> str:
    start = max(0, offset - 64)
    handle.seek(start)
    tail = handle.read(offset - start)
    if len(tail) != offset - start:
        raise CollectionError("traffic log was truncated before its checkpoint")
    return hashlib.sha256(tail).hexdigest()


def read_stream(
    handle: BinaryIO,
    checkpoints: dict[str, dict[str, Any]],
    budget: int,
    seen: set[str],
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
        record = usage(line)
        count(buckets, record)
        track_path(paths.setdefault((record.host, record.hour), {}), record)
        consumed += len(line)
    if not consumed:
        return None, 0, more
    end = offset + consumed
    for key, bucket in buckets.items():
        bucket["paths"] = top_paths(paths.get(key, {}))
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
                    cast(BinaryIO, handle), checkpoints, budget, seen
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
