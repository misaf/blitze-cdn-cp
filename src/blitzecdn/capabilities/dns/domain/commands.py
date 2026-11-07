"""Immutable policy-editing intent from the entry points.

Replacing settings and editing selected lists are different commands: the
latter can only expand to a replacement once the service has read the current
zone inside its transaction. Edits of one contract's lists live with that
contract's capability and satisfy :class:`PolicyCommand`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, field_validator

from blitzecdn.capabilities.dns.domain.patch import DomainPatch
from blitzecdn.core.domain.documents import canonical_document, read_document
from blitzecdn.core.features.types import PolicyView


class PatchPolicy(BaseModel):
    """Replace named settings, immutably; explicit nulls stay distinct from unset."""

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

    def changes(self, current: PolicyView) -> dict[str, Any]:
        return read_document(self.settings_json)


class PolicyCommand(Protocol):
    """An edit to a zone's policy, resolved against the policy it edits.

    `PatchPolicy` is DNS's; a capability whose contract needs a finer edit,
    such as security's firewall lists, declares its own beside the contract.
    """

    def changes(self, current: PolicyView) -> Mapping[str, Any]: ...


__all__ = ["PatchPolicy", "PolicyCommand"]
