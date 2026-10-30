"""The delivery projection of zones and virtual hosts: explicit, setting by setting.

Storage and release inputs serialize the nested domain values instead.
"""

from typing import Any

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.core.domain.site import CdnSite


def serving_document(value: Domain | CdnSite) -> dict[str, Any]:
    return value.policy.settings(mode="json") | value.model_dump(
        mode="json", exclude={"policy"}
    )
