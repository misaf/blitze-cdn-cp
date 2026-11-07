"""Measure the edge traffic collector's sustained throughput (`just bench-traffic`).

Writes realistic hostname JSON access logs, plain and gzip, then drains them
with the shipped standalone collector exactly as the scheduled job would: one
call per pass, each bounded by ``max_bytes``, resuming from the cursors the
previous pass returned. Reports MiB/s and records/s on one core, which is what
`docs/traffic-accounting.md` sizes the collection budget from.

    uv run python tests/traffic_collector_benchmark.py [--records N] [--max-bytes B]
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import runpy
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, cast

COLLECTOR = (
    Path(__file__).resolve().parents[1]
    / "src/blitzecdn/capabilities/traffic/ansible/roles"
    / "blitzecdn_traffic_collect/files/collect_traffic.py"
)
OUTCOMES = ("HIT",) * 7 + ("MISS", "MISS", "EXPIRED", "BYPASS", "")


def record(rng: random.Random, hostname: str, when: datetime) -> bytes:
    """One line shaped like the edge's log_format, about 500-600 bytes."""
    path = "/" + "/".join(uuid.UUID(int=rng.getrandbits(128)).hex[:8] for _ in range(4))
    sent = rng.randint(300, 2_000_000)
    return (
        json.dumps(
            {
                "schema_version": 1,
                "request_id": uuid.UUID(int=rng.getrandbits(128)).hex,
                "timestamp": when.isoformat(timespec="seconds"),
                "hostname": hostname,
                "server_name": hostname,
                "bytes_sent": sent,
                "body_bytes_sent": max(sent - 300, 0),
                "status": 200,
                "request_time": 0.012,
                "client_ip": "2001:db8::1",
                "request": f"GET {path}?v={rng.randint(1, 99)} HTTP/2.0",
                "cache_status": rng.choice(OUTCOMES),
                "upstream_address": "-",
                "upstream_response_time": "-",
                "referer": f"https://{hostname}/",
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def write_logs(directory: Path, records: int, *, compressed: bool) -> int:
    rng = random.Random(1)  # noqa: S311 - reproducible fixtures, not secrets
    hosts = [f"site{index}.example.com" for index in range(8)]
    start = datetime(2026, 10, 27, tzinfo=UTC)
    size = 0
    with ExitStack() as stack:
        handles: dict[str, IO[bytes]] = {}
        for host in hosts:
            path = directory / (f"{host}.access.log.1" + (".gz" if compressed else ""))
            handle = (
                gzip.open(path, "wb", compresslevel=6)  # noqa: SIM115
                if compressed
                else path.open("wb")
            )
            handles[host] = stack.enter_context(cast("IO[bytes]", handle))
        for index in range(records):
            host = hosts[index % len(hosts)]
            line = record(rng, host, start + timedelta(seconds=index // 50))
            handles[host].write(line)
            size += len(line)
    return size


def drain(
    collect: Callable[[dict[str, Any]], dict[str, Any]], directory: Path, budget: int
) -> tuple[int, int]:
    """Run passes until nothing is left; return (passes, records)."""
    cursors: dict[str, dict[str, Any]] = {}
    passes = records = 0
    while True:
        result = collect(
            {
                "log_directory": str(directory),
                "max_bytes": budget,
                "checkpoints": list(cursors.values()),
            }
        )
        if result["errors"]:
            raise SystemExit(f"collector errors: {result['errors']}")
        passes += 1
        for batch in result["batches"]:
            cursors[batch["end"]["stream_id"]] = batch["end"]
            records += sum(bucket["requests"] for bucket in batch["buckets"])
        if not result["more"]:
            return passes, records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=int, default=200_000)
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    arguments = parser.parse_args()
    collect = runpy.run_path(str(COLLECTOR))["collect"]
    for compressed in (False, True):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            size = write_logs(directory, arguments.records, compressed=compressed)
            began = time.perf_counter()
            passes, counted = drain(collect, directory, arguments.max_bytes)
            elapsed = time.perf_counter() - began
        assert counted == arguments.records, (counted, arguments.records)
        label = "gzip " if compressed else "plain"
        print(
            f"{label}: {counted} records, {size / 2**20:.1f} MiB in {passes} passes, "
            f"{elapsed:.2f}s -> {size / 2**20 / elapsed:.1f} MiB/s, "
            f"{counted / elapsed:,.0f} records/s, {size / counted:.0f} B/record"
        )


if __name__ == "__main__":
    main()
