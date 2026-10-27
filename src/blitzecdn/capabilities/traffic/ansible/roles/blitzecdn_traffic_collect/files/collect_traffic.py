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
from typing import Any, BinaryIO, cast

MAX_LINE_BYTES = 65536
MAX_BATCHES = 128
MAX_COUNTER = 2**63 - 1
LOG_NAME = re.compile(r"^[a-z0-9_.-]+\.access\.log(?:\.\d+)?(?:\.gz)?$")
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


class CollectionError(ValueError):
    """A safe diagnostic containing no raw request data."""


def usage(line: bytes) -> tuple[str, str, int, int]:
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
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        )
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
    return host, hour, cast(int, counters[0]), cast(int, counters[1])


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
    while True:
        line = handle.readline(MAX_LINE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_LINE_BYTES:
            raise CollectionError("traffic record exceeds the line limit")
        if not line.endswith(b"\n") or consumed + len(line) > budget:
            more = True
            break
        host, hour, sent, body = usage(line)
        bucket = buckets.setdefault(
            (host, hour),
            {
                "hostname": host,
                "hour": hour,
                "requests": 0,
                "bytes_sent": 0,
                "body_bytes_sent": 0,
            },
        )
        bucket["requests"] += 1
        bucket["bytes_sent"] += sent
        bucket["body_bytes_sent"] += body
        if any(
            bucket[key] > MAX_COUNTER
            for key in ("requests", "bytes_sent", "body_bytes_sent")
        ):
            raise CollectionError("hourly traffic counter exceeds the integer range")
        consumed += len(line)
    if not consumed:
        return None, 0, more
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
    budget = document.get("max_bytes", 4 * 1024 * 1024)
    if type(budget) is not int or not MAX_LINE_BYTES <= budget <= 64 * 1024 * 1024:
        raise CollectionError("max_bytes must be between 65536 and 67108864")
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
    return result


def main() -> None:
    json.dump(collect(json.load(sys.stdin)), sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
