"""Partial zone policy updates, also used to validate rule overrides.

Unset fields are untouched; the merged zone is validated as a whole. The fields
are the composed policy's flat settings, each optional, so a contributed
setting is patchable the moment its contract is registered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Self

from pydantic import ConfigDict
from pydantic.fields import FieldInfo

from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.tls.contract import CertificateMode
from blitzecdn.core.domain.composed import ComposedModel

__all__ = ["DomainPatch", "reject_issuer_owned_certificate", "setting_annotation"]


def reject_issuer_owned_certificate(changes: Mapping[str, Any]) -> None:
    """Refuse an operator edit that claims controller-managed TLS material.

    Called only from the operator authoring paths. The issuer writes a rule's
    certificate as an overrides mapping carrying ``requested`` through
    ``replace_rule``, so the check is about who sets the value, which only the
    entry point knows. Without it the zone's hostnames go silently dark until
    ``blitzecdn validate`` names them.
    """
    mode = changes.get("certificate_mode")
    if mode is None:
        return
    if CertificateMode(mode).issuer_owned:
        raise ValueError(
            f"certificate_mode={CertificateMode(mode).value!r} is set by the "
            "certificate upload and request endpoints, which own the material "
            "and the paths it lives under; an operator sets 'disabled' or "
            "'existing'"
        )


def setting_annotation(field: FieldInfo) -> Any:
    """A setting's type with its constraints, such as ``ge=0``, kept.

    A patch is validated on its own before it is merged, and a rule's overrides
    are stored without the merged policy being validated again, so a patch that
    took the bare type would accept a value the contract refuses.
    """
    if not field.metadata:
        return field.annotation
    return Annotated[field.annotation, *field.metadata]


class DomainPatch(ComposedModel):
    """A partial update to a zone: every setting optional, unset means untouched.

    Blocks such as ``firewall`` and ``visitor_headers`` replace wholesale:
    ``{"firewall": {}}`` clears every rule, and ``{"visitor_headers": {}}``
    restores the defaults rather than keeping the current switches.
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def composition_source(cls) -> object:
        return SitePolicy.policy_schema()

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        # `source` is the schema `setting_fields` reads.
        return cls.compose_fields(
            {
                name: (setting_annotation(field) | None, None)
                for name, field in SitePolicy.setting_fields().items()
            }
        )
