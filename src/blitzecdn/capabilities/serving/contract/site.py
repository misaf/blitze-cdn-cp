"""Compose serving policy independently of DNS storage and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from blitzecdn.capabilities.cache.contract import CachePolicy
from blitzecdn.capabilities.compression.contract import CompressionPolicy
from blitzecdn.capabilities.http.contract import ProtocolPolicy
from blitzecdn.capabilities.security.contract import SecurityPolicy
from blitzecdn.capabilities.serving.contract.headers import HeaderPolicy
from blitzecdn.capabilities.serving.contract.origin import OriginPolicy
from blitzecdn.capabilities.tls.contract import TlsPolicy
from blitzecdn.core.domain.policy import CapabilityPolicy


class SitePolicy(BaseModel):
    """One serving policy composed from independently validated contracts.

    Each contract is a named field, not a base class. Flat settings are only an
    edit and edge-document format, never the stored shape.
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
        """The declared contract fields, read from the model itself."""
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
        """Decode the flat format, explicitly; unknown names fail rather than vanish."""
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
        """Revalidate named settings, keeping every unrelated value.

        Leaf blocks such as firewall and visitor headers replace wholesale.
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
