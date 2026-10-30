"""Serving policy contracts shared by zones and effective virtual hosts."""

from blitzecdn.capabilities.serving.contract.headers import (
    HeaderPolicy,
    SiteVisitorHeaders,
)
from blitzecdn.capabilities.serving.contract.origin import OriginPolicy
from blitzecdn.capabilities.serving.contract.site import SitePolicy

__all__ = [
    "HeaderPolicy",
    "OriginPolicy",
    "SitePolicy",
    "SiteVisitorHeaders",
]
