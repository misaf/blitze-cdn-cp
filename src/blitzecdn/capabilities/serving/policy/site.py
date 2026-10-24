"""Compose serving policy independently of DNS storage and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from blitzecdn.capabilities.cache.policy import CachePolicy
from blitzecdn.capabilities.compression.policy import CompressionPolicy
from blitzecdn.capabilities.http.policy import ProtocolPolicy
from blitzecdn.capabilities.security.policy import SecurityPolicy
from blitzecdn.capabilities.serving.policy.headers import HeaderPolicy
from blitzecdn.capabilities.serving.policy.origin import OriginPolicy
from blitzecdn.capabilities.tls.policy import TlsPolicy
from blitzecdn.core.domain.policy import CapabilityPolicy


class SitePolicy(BaseModel):
    """One serving policy composed from independently validated contracts.

    Policy ownership is a named field rather than a position in an inheritance
    order. Zones and derived hosts hold this value; neither is a kind of TLS,
    caching or security policy. Flat settings are an explicit boundary format
    used by editing commands and edge documents, never the stored model shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    tls: TlsPolicy = Field(default_factory=TlsPolicy)
    protocols: ProtocolPolicy = Field(default_factory=ProtocolPolicy)
    origin: OriginPolicy = Field(default_factory=OriginPolicy)
    cache: CachePolicy = Field(default_factory=CachePolicy)
    compression: CompressionPolicy = Field(default_factory=CompressionPolicy)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)
    headers: HeaderPolicy = Field(default_factory=HeaderPolicy)

    @classmethod
    def component_types(cls) -> Mapping[str, type[CapabilityPolicy]]:
        """The declared value fields, with no parallel registry to drift.

        A new contract participates by being an explicitly typed nested field.
        Reading those fields is ordinary composition; inspecting base classes
        would let inheritance accidentally add behavior without a value owner.
        """
        components: dict[str, type[CapabilityPolicy]] = {}
        for name, field in cls.model_fields.items():
            if name == "enabled":
                continue
            contract = field.annotation
            if not isinstance(contract, type) or not issubclass(
                contract, CapabilityPolicy
            ):
                raise TypeError(f"policy component {name!r} must be a CapabilityPolicy")
            components[name] = contract
        return components

    @classmethod
    def setting_fields(cls) -> Mapping[str, FieldInfo]:
        """Field definitions of the flat editing/edge format, checked unique."""
        fields = {"enabled": cls.model_fields["enabled"]}
        for contract in cls.component_types().values():
            for name, field in contract.model_fields.items():
                if name in fields:
                    raise TypeError(f"serving setting {name!r} has multiple owners")
                fields[name] = field
        return fields

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> Self:
        """Decode the flat command or edge format at an explicit boundary.

        This is not a model validator: ordinary constructors and stored JSON
        accept only nested values. Unknown names must fail rather than vanish
        while partitioning a settings mapping between its owners.
        """
        unknown = sorted(set(settings) - set(cls.setting_fields()))
        if unknown:
            raise ValueError("unknown serving settings: " + ", ".join(unknown))
        document: dict[str, Any] = {}
        if "enabled" in settings:
            document["enabled"] = settings["enabled"]
        for group, contract in cls.component_types().items():
            values = {
                name: settings[name]
                for name in contract.model_fields
                if name in settings
            }
            if values:
                document[group] = values
        return cls.model_validate(document)

    def settings(self, *, mode: Literal["json", "python"] = "python") -> dict[str, Any]:
        """Project policy into flat command/edge settings, never persistence."""
        document: dict[str, Any] = {"enabled": self.enabled}
        for name in self.component_types():
            component: CapabilityPolicy = getattr(self, name)
            document.update(
                component.model_dump(mode="json")
                if mode == "json"
                else {
                    field: getattr(component, field)
                    for field in type(component).model_fields
                }
            )
        return document

    def applying(self, changes: Mapping[str, Any]) -> Self:
        """Revalidate named settings while retaining every unrelated value.

        Leaf blocks such as firewall and visitor headers replace wholesale,
        preserving editing semantics. The capability containers themselves are
        never blindly replaced with partial dictionaries.
        """
        return type(self).from_settings(self.settings() | dict(changes))

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Collect requirements from contained values, retaining setting names."""
        if not self.enabled:
            return {}
        requested: dict[str, list[str]] = {}
        for name in self.component_types():
            component: CapabilityPolicy = getattr(self, name)
            for token, settings in component.capability_requirements.items():
                requested.setdefault(token, []).extend(settings)
        return {name: tuple(settings) for name, settings in requested.items()}

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset(self.capability_requirements)

    @model_validator(mode="after")
    def validate_setting_ownership(self) -> Self:
        self.setting_fields()
        return self

    @model_validator(mode="after")
    def validate_http3_requires_edge_tls(self) -> Self:
        if self.protocols.http3_enabled and not self.tls.ssl_mode.serves_tls:
            raise ValueError("http3_enabled=True requires ssl_mode to serve edge TLS")
        return self

    @property
    def requires_geoip(self) -> bool:
        return (
            self.security.firewall.requires_geoip
            or self.headers.visitor_headers.requires_geoip
        )
