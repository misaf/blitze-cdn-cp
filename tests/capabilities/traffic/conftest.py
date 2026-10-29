import json
import runpy
import uuid

import pytest
from support.paths import SOURCE


@pytest.fixture
def collector():
    path = (
        SOURCE
        / "capabilities/traffic/ansible/roles"
        / "blitzecdn_traffic_collect/files/collect_traffic.py"
    )
    return runpy.run_path(str(path))["collect"]


@pytest.fixture
def record():
    def make(**overrides):
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": uuid.uuid4().hex,
                    "hostname": "api.example.com",
                    "timestamp": "2026-10-27T13:12:00+03:30",
                    "bytes_sent": 120,
                    "body_bytes_sent": 100,
                    **overrides,
                }
            )
            + "\n"
        ).encode()

    return make
