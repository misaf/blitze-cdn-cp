"""Generated logs: whatever visitors put in them, collection keeps up.

Hostnames, request lines and line lengths in an access log come from visitors.
Any mix of good records and bad ones, anywhere in a file, must be read to the
end in one run: good records counted, bad ones counted as skipped, and the
checkpoint left at the end of the file, so the next run has nothing to redo.
"""

from __future__ import annotations

import json
import re
import runpy
import tempfile
import uuid
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from support.paths import SOURCE

COLLECTOR = runpy.run_path(
    str(
        SOURCE
        / "capabilities/traffic/ansible/roles"
        / "blitzecdn_traffic_collect/files/collect_traffic.py"
    )
)
COLLECT = COLLECTOR["collect"]
MAX_LINE = COLLECTOR["MAX_LINE_BYTES"]
REQUEST_ID = re.compile(r"[0-9a-f]{32}")

_LABEL = st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,10}[a-z0-9])?", fullmatch=True)
_HOSTNAME = st.lists(_LABEL, min_size=1, max_size=4).map(".".join)
_COUNTER = st.integers(0, 2**40)


def _line(record: dict[str, Any]) -> bytes:
    return json.dumps(record).encode() + b"\n"


@st.composite
def _good(draw: st.DrawFn) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": 1,
        "request_id": uuid.UUID(int=draw(st.integers(0, 2**128 - 1))).hex,
        "hostname": draw(_HOSTNAME),
        "timestamp": draw(
            st.sampled_from(
                [
                    "2026-10-27T13:12:00+03:30",
                    "2026-10-27T10:59:59Z",
                    "2026-01-01T00:00:00-12:00",
                ]
            )
        ),
        "bytes_sent": draw(_COUNTER),
        "body_bytes_sent": draw(_COUNTER),
    }
    # What a visitor chooses rides along, and must never stop a good record.
    optional = {
        "status": st.one_of(st.integers(-1000, 1000), st.text(max_size=5), st.none()),
        "cache_status": st.one_of(st.text(max_size=12), st.none()),
        "request": st.text(max_size=80),
        "user_agent": st.text(max_size=80),
    }
    for name, strategy in optional.items():
        if draw(st.booleans()):
            record[name] = draw(strategy)
    return record


#: Hostnames the collector must refuse: a character outside the label
#: alphabet, an empty label, a label edge hyphen, or too long overall.
_BAD_HOSTNAME = st.one_of(
    st.text(min_size=1, max_size=40).filter(
        lambda name: any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in name
        )
    ),
    st.sampled_from(["", ".", "a..b", "-a.example.com", "a-.example.com", "a" * 64]),
    _LABEL.map(lambda label: ".".join([label] * (254 // (len(label) + 1) + 1))),
)
_BAD_COUNTER = st.one_of(
    st.integers(max_value=-1),
    st.integers(min_value=2**63),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=5),
    st.booleans(),
    st.none(),
)


@st.composite
def _bad(draw: st.DrawFn) -> bytes:
    """One line the collector must skip, ending in its newline."""
    record = draw(_good())
    kind = draw(
        st.sampled_from(
            ["hostname", "counter", "request_id", "schema", "timestamp", "long", "junk"]
        )
    )
    if kind == "hostname":
        record["hostname"] = draw(_BAD_HOSTNAME)
    elif kind == "counter":
        record[draw(st.sampled_from(["bytes_sent", "body_bytes_sent"]))] = draw(
            _BAD_COUNTER
        )
    elif kind == "request_id":
        record["request_id"] = draw(
            st.text(max_size=40).filter(lambda text: not REQUEST_ID.fullmatch(text))
        )
    elif kind == "schema":
        record["schema_version"] = draw(st.sampled_from([0, 2, "1", 1.0, None]))
    elif kind == "timestamp":
        record["timestamp"] = draw(
            st.sampled_from(["", "yesterday", "2026-10-27T13:12:00", None, 1730000000])
        )
    elif kind == "long":
        record["user_agent"] = "x" * draw(st.integers(MAX_LINE, MAX_LINE * 3))
    else:
        return (
            draw(st.binary(max_size=200).filter(lambda raw: b"\n" not in raw)) + b"\n"
        )
    return _line(record)


#: Each line, and whether it was generated as one to count.
_LINES = st.lists(
    st.one_of(
        _good().map(lambda record: (_line(record), True)),
        _bad().map(lambda line: (line, False)),
    ),
    max_size=25,
)


@settings(max_examples=100, deadline=None)
@given(first=_good(), rest=_LINES)
def test_any_mix_of_records_is_read_to_the_end_in_one_run(first, rest):
    """The first record names the stream, so it is always a good one here."""
    lines = [_line(first), *(line for line, _ in rest)]
    good = 1 + sum(counted for _, counted in rest)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "_wildcard.example.com.access.log"
        path.write_bytes(b"".join(lines))

        result = COLLECT({"log_directory": directory, "checkpoints": []})

        assert result["errors"] == []
        assert result["more"] is False
        (batch,) = result["batches"]
        assert batch["end"]["offset"] == path.stat().st_size
        assert sum(bucket["requests"] for bucket in batch["buckets"]) == good
        assert batch["skipped"] == len(lines) - good
        # Nothing the visitor wrote is echoed back.
        assert "x" * 64 not in json.dumps(result)

        again = COLLECT({"log_directory": directory, "checkpoints": [batch["end"]]})
        assert again["errors"] == [] and again["batches"] == []
