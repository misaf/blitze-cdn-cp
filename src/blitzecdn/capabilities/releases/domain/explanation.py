"""Why one derived host is served the way it is, setting by setting.

Knowing which rule won does not say where each value came from: a winning rule
that overrides nothing relevant leaves the zone's value. Each setting names
``RULE`` (the rule set it), ``ZONE`` (the zone set it) or ``DEFAULT`` (nobody
did). ``ZONE`` versus ``DEFAULT`` compares against the declared default, so
setting a value to what it already was changes nothing observable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.dns.domain import Domain, Rule
from blitzecdn.core.domain.site_policy import SitePolicy

__all__ = ["HostExplanation", "SettingOrigin", "SettingSource", "explain"]


class SettingOrigin(StrEnum):
    """Where an effective value came from. Ordered most specific first."""

    RULE = "rule"
    ZONE = "zone"
    DEFAULT = "default"


class SettingSource(BaseModel):
    """One effective setting, and what put it there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    setting: str
    #: JSON-mode, because this travels through the API and into a stored
    #: release document, and a policy value may be an enum or a nested model.
    value: object
    origin: SettingOrigin
    #: Set only when ``origin`` is ``RULE``; a value a rule left alone is the zone's.
    rule: str | None = None


class HostExplanation(BaseModel):
    """Every effective setting for one derived host, which covers all its hostnames."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The derived host name, which is also its certificate directory.
    host: str
    zone: str
    #: The rule that won for this group, or ``None`` for the zone's own policy.
    rule: str | None
    server_names: tuple[str, ...]
    origin_host: str
    settings: tuple[SettingSource, ...]


def _declared_defaults() -> dict[str, object]:
    """Each policy field's schema default, in the zone editor's vocabulary."""
    return SitePolicy().settings(mode="json")


def explain(
    zone: Domain,
    rule: Rule | None,
    *,
    host: str,
    server_names: tuple[str, ...],
    origin_host: str,
) -> HostExplanation:
    """Attribute every effective setting of one derived host to its source.

    Takes the zone and rule separately, since merging erases the attribution.
    """
    defaults = _declared_defaults()
    overrides = dict(rule.overrides) if rule is not None else {}
    merged = zone.policy.applying(overrides)
    document = merged.settings(mode="json")

    settings: list[SettingSource] = []
    for setting in sorted(defaults):
        value = document[setting]
        if setting in overrides:
            origin, decided_by = SettingOrigin.RULE, (rule.name if rule else None)
        elif value != defaults[setting]:
            origin, decided_by = SettingOrigin.ZONE, None
        else:
            origin, decided_by = SettingOrigin.DEFAULT, None
        settings.append(
            SettingSource(setting=setting, value=value, origin=origin, rule=decided_by)
        )
    return HostExplanation(
        host=host,
        zone=zone.name,
        rule=rule.name if rule is not None else None,
        server_names=server_names,
        origin_host=origin_host,
        settings=tuple(settings),
    )
