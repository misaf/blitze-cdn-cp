"""Compose serving policy from the contracts the features contribute.

`SitePolicy` names no contract. The fields come from
:class:`~blitzecdn.core.features.PolicyContribution`, fixed once by
:meth:`~blitzecdn.capabilities.serving.contract.SitePolicy.seal` when the
registry loads, and a contract is read with ``policy[TlsPolicy]``. The class is
a :class:`~blitzecdn.core.domain.composed.ComposedModel`, so a field, body or
response can name it at import time, before the registry exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal, Self, cast

from pydantic import ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from blitzecdn.core.domain.composed import ComposedModel
from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.resolution import ResolvedPolicySchema

__all__ = ["SitePolicy"]


class SitePolicy(ComposedModel):
    """One serving policy composed from independently validated contracts.

    Each contract is a field named by its group. Flat settings are only an
    edit and edge-document format, never the stored shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    _schema: ClassVar[ResolvedPolicySchema | None] = None

    enabled: bool = True

    # --- composition ---------------------------------------------------------

    @classmethod
    def seal(cls, schema: ResolvedPolicySchema) -> None:
        """Compose the policy from ``schema``; an equal schema keeps the model."""
        if SitePolicy._schema != schema:
            SitePolicy._schema = schema

    @staticmethod
    def policy_schema() -> ResolvedPolicySchema:
        """The contributions this policy is composed from."""
        if SitePolicy._schema is None:
            raise FeatureError(
                "the site policy is not composed yet: load the feature registry "
                "(`blitzecdn.composition.load_registry`) before validating a "
                "policy; the test suite does so in tests/conftest.py"
            )
        return SitePolicy._schema

    @classmethod
    def composition_source(cls) -> object:
        return SitePolicy.policy_schema()

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        schema = cast(ResolvedPolicySchema, source)
        return cls.compose_fields(
            {
                group: (contract, Field(default_factory=contract))
                for group, contract in schema.groups.items()
            }
        )

    def __getitem__[P: CapabilityPolicy](self, contract: type[P]) -> P:
        """The contributed contract of type ``contract``."""
        # Validation put an instance of the group's contract there.
        return cast(P, getattr(self, SitePolicy.policy_schema().group_of(contract)))

    # --- the flat format -----------------------------------------------------

    @classmethod
    def component_types(cls) -> Mapping[str, type[CapabilityPolicy]]:
        """Every contributed contract, by group."""
        return SitePolicy.policy_schema().groups

    @classmethod
    def setting_fields(cls) -> Mapping[str, FieldInfo]:
        """Field definitions of the flat editing/edge format."""
        fields = {"enabled": SitePolicy.model_fields["enabled"]}
        for contract in cls.component_types().values():
            fields.update(contract.model_fields)
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

    def components(self) -> Mapping[str, CapabilityPolicy]:
        """Every contract this policy holds, by group."""
        return {group: getattr(self, group) for group in self.component_types()}

    def settings(self, *, mode: Literal["json", "python"] = "python") -> dict[str, Any]:
        """Project policy into flat command/edge settings, never persistence."""
        document: dict[str, Any] = {"enabled": self.enabled}
        for component in self.components().values():
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
        for component in self.components().values():
            for token, settings in component.capability_requirements.items():
                requested.setdefault(token, []).extend(settings)
        return {name: tuple(settings) for name, settings in requested.items()}

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset(self.capability_requirements)

    def validate_for_site(self, name: str) -> None:
        """The contributed rules that need the host's name; raises ``ValueError``."""
        for check in SitePolicy.policy_schema().site_checks:
            check(name, self)

    @model_validator(mode="after")
    def validate_contributed_checks(self) -> Self:
        if self.__composed__:
            for check in SitePolicy.policy_schema().checks:
                check(self)
        return self
