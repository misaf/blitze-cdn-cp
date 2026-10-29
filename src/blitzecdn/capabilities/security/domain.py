"""Editing selected firewall lists, resolved against the zone's current block."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

from blitzecdn.capabilities.security.contract import SecurityPolicy, SiteFirewall
from blitzecdn.core.features.types import PolicyView

__all__ = ["EditFirewall"]


class EditFirewall(BaseModel):
    """Replace selected rule lists, or clear the block; validated after the merge."""

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

    def changes(self, current: PolicyView) -> dict[str, Any]:
        lists = self.model_dump(exclude_none=True, exclude={"clear"})
        firewall = (
            SiteFirewall()
            if self.clear
            else current[SecurityPolicy].firewall.replacing(lists)
        )
        return {"firewall": firewall}
