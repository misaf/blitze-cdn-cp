"""Explicit delivery projection of composed zone and virtual-host values.

API, CLI and edge documents name individual settings. Storage and release
inputs serialize the nested domain values instead; delivery never relies on a
custom model serializer to make those two representations look identical.
"""

from typing import Any

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.serving.domain import CdnSite


def serving_document(value: Domain | CdnSite) -> dict[str, Any]:
    return value.policy.settings(mode="json") | value.model_dump(
        mode="json", exclude={"policy"}
    )
