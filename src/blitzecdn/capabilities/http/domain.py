"""Editing selected visitor-header switches, keeping the others as they are."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

from blitzecdn.capabilities.http.contract import HeaderPolicy, SiteVisitorHeaders
from blitzecdn.core.features.types import PolicyView

__all__ = ["EditVisitorHeaders"]


class EditVisitorHeaders(BaseModel):
    """Change selected switches while preserving the other current switches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connecting_ip: bool | None = None
    ip_country: bool | None = None

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        if self.connecting_ip is None and self.ip_country is None:
            raise ValueError("a visitor-header edit requires at least one switch")
        return self

    def changes(self, current: PolicyView) -> dict[str, Any]:
        switches = self.model_dump(exclude_none=True)
        headers = SiteVisitorHeaders.model_validate(
            current[HeaderPolicy].visitor_headers.model_dump() | switches
        )
        return {"visitor_headers": headers}
