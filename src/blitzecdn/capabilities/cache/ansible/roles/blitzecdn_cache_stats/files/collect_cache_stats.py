#!/usr/bin/env python3
"""Summarize recent hostname access-log records as JSON, for the control plane.

Run through Ansible's script module, which copies this to a temporary
directory, executes it, and deletes it — the edge keeps no part of it. The
counting happens here rather than on the controller because a busy edge logs
gigabytes a day and shipping that back to be counted would cost far more than
the answer is worth.

Reads the live JSON Lines files blitzecdn_nginx writes per configured hostname
(``<hostname>.access.log``); rotated archives are history, not "now". Each file
contributes its own newest lines, so one busy site cannot push a quiet one out
of the window.

Usage: collect_cache_stats.py <log-directory> <max-lines-per-log>
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

BLOCK_BYTES = 65536


def tail(path: Path, max_lines: int) -> list[bytes]:
    """The newest complete lines of a file, reading backwards from its end."""
    # Each block is scanned and copied once, so the cost is linear in the
    # bytes the window needs rather than quadratic in them.
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        blocks: list[bytes] = []
        newlines = 0
        while position > 0 and newlines <= max_lines:
            step = min(BLOCK_BYTES, position)
            position -= step
            stream.seek(position)
            block = stream.read(step)
            newlines += block.count(b"\n")
            blocks.append(block)
    lines = b"".join(reversed(blocks)).split(b"\n")
    # The last element follows the final newline: empty, or a record nginx is
    # still writing. The first is a partial line unless the read began at 0.
    lines.pop()
    if position > 0 and lines:
        lines.pop(0)
    return lines[-max_lines:] if max_lines > 0 else []


def collect(directory: Path, max_lines: int) -> list[dict[str, object]]:
    counts: Counter[tuple[str, str]] = Counter()
    # A directory or file that does not exist yet is an ordinary state: nginx
    # creates them on first use. Reporting no requests lets a fresh edge appear
    # in the output as itself rather than failing the whole run.
    paths = sorted(directory.glob("*.access.log")) if directory.is_dir() else []
    for path in paths:
        try:
            lines = tail(path, max_lines)
        except FileNotFoundError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            host = record.get("hostname")
            if not isinstance(host, str) or host in ("", "-"):
                host = "unknown"
            # A request that never consulted the cache logs an empty value — a
            # redirect, an error nginx served itself, or a site with
            # cache_enabled false. Counted as NONE and kept out of the ratio
            # rather than scored as a miss, which would make a working cache
            # look broken on a redirect-heavy site.
            outcome = record.get("cache_status")
            if not isinstance(outcome, str) or outcome in ("", "-"):
                outcome = "NONE"
            counts[host, outcome] += 1
    return [
        {"site": host, "outcome": outcome, "requests": requests}
        for (host, outcome), requests in sorted(counts.items())
    ]


if __name__ == "__main__":
    json.dump(
        collect(Path(sys.argv[1]), int(sys.argv[2])),
        sys.stdout,
        separators=(",", ":"),
    )
