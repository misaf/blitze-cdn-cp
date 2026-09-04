"""The operational shapes this capability adds to the published API document.

``tests/api/test_surface.py`` pins what core publishes. These two components
exist only while this distribution is attached, so pinning them there made the
whole table unassertable in the core-only workspace — the test was held by name
in a ``REQUIRES_CERTIFICATES`` set and skipped outright, which left core's eight
shapes unchecked along with these two. Each distribution pins its own.
"""

from __future__ import annotations

from control_plane_fixtures import control_plane_app
from fastapi.testclient import TestClient

#: Same form as core's table: field names, then required fields, each sorted
#: and space-joined so a diff names the field that moved.
PUBLISHED_CERTIFICATE_SHAPES = {
    "ReconciliationResult": ("deployment failed issued skipped", ""),
    "SslAutomaticReconciliation": ("deployment scanned skipped upgraded", ""),
}


def test_published_certificate_shapes_are_pinned(settings):
    with TestClient(control_plane_app(settings)) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    actual = {
        name: (
            " ".join(sorted(schemas[name]["properties"])),
            " ".join(sorted(schemas[name].get("required", ()))),
        )
        if name in schemas
        else ("", "")
        for name in PUBLISHED_CERTIFICATE_SHAPES
    }
    assert actual == PUBLISHED_CERTIFICATE_SHAPES
