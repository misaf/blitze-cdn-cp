"""Build domain values from flat editing/edge settings in integration fixtures.

Production models accept nested policy only. Tests describing API settings or
Ansible payloads use this explicit conversion instead of making model validation
accept a second, ambiguous storage format.
"""

from collections.abc import Mapping
from typing import Any

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.serving.policy import SitePolicy


def _document(values: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(values)
    changes = {
        name: document.pop(name)
        for name in SitePolicy.setting_fields()
        if name in document
    }
    policy = document.pop("policy", SitePolicy())
    if not isinstance(policy, SitePolicy):
        policy = SitePolicy.model_validate(policy)
    return document | {"policy": policy.applying(changes)}


def site_from_settings(values: Mapping[str, Any]) -> CdnSite:
    return CdnSite.model_validate(_document(values))


def domain_from_settings(values: Mapping[str, Any]) -> Domain:
    return Domain.model_validate(_document(values))
