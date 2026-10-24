"""Immutable policy-editing intent shared by application entry points.

A replacement patch and an edit to selected lists are different commands. The
latter cannot be expanded to a replacement until the application has read the
current zone inside its write transaction. Delivery supplies intent only.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from blitzecdn.capabilities.dns.domain.patch import DomainPatch
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.serving.policy import SitePolicy, SiteVisitorHeaders
from blitzecdn.core.domain.documents import canonical_document, read_document


class PatchPolicy(BaseModel):
    """Replace named settings, including whole firewall/header leaf blocks.

    Capture the patch as canonical content: a caller retaining a mutable
    DomainPatch or input mapping cannot change an already constructed command.
    Explicit nulls survive and remain distinct from fields left unnamed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    settings_json: str

    @field_validator("settings_json", mode="before")
    @classmethod
    def validate_settings(cls, value: object) -> str:
        content = canonical_document(value)
        patch = DomainPatch.model_validate(read_document(content))
        return canonical_document(patch.model_dump(mode="json", exclude_unset=True))

    @classmethod
    def from_patch(cls, patch: DomainPatch) -> Self:
        return cls(
            settings_json=canonical_document(
                patch.model_dump(mode="json", exclude_unset=True)
            )
        )

    def changes(self, current: SitePolicy) -> dict[str, Any]:
        return read_document(self.settings_json)


class EditFirewall(BaseModel):
    """Replace selected rule lists, or explicitly clear the whole block.

    Tuple fields keep admitted intent immutable. Cross-list validation belongs
    to the merged SiteFirewall, after the application reads current state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_sources: tuple[str, ...] | None = None
    deny_sources: tuple[str, ...] | None = None
    allowed_countries: tuple[str, ...] | None = None
    denied_countries: tuple[str, ...] | None = None
    denied_methods: tuple[str, ...] | None = None
    denied_paths: tuple[str, ...] | None = None
    clear: bool = False

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        named = self.model_dump(exclude_none=True, exclude={"clear"})
        if self.clear and named:
            raise ValueError("clear cannot be combined with named firewall lists")
        if not self.clear and not named:
            raise ValueError("a firewall edit requires named lists or clear")
        return self

    def changes(self, current: SitePolicy) -> dict[str, Any]:
        lists = self.model_dump(exclude_none=True, exclude={"clear"})
        firewall = (
            SiteFirewall() if self.clear else current.security.firewall.replacing(lists)
        )
        return {"firewall": firewall}


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

    def changes(self, current: SitePolicy) -> dict[str, Any]:
        switches = self.model_dump(exclude_none=True)
        headers = SiteVisitorHeaders.model_validate(
            current.headers.visitor_headers.model_dump() | switches
        )
        return {"visitor_headers": headers}


type PolicyCommand = PatchPolicy | EditFirewall | EditVisitorHeaders

__all__ = ["EditFirewall", "EditVisitorHeaders", "PatchPolicy", "PolicyCommand"]
