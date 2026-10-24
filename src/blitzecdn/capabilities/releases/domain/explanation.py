"""Why one derived host is served the way it is, setting by setting.

"Why is this hostname not caching" is the question operators actually arrive
with, and until now the control plane could answer only half of it. The
``/resolve`` endpoint names the rule that won, which settles the match; it does
not settle where any individual value came from, and the two are not the same
question. A rule that wins and overrides nothing relevant leaves the zone's
value in place, and an operator who reads only "rule ``api`` applied" goes and
edits the rule.

So an explanation is per setting, and it names one of three origins:

``RULE``
    the winning rule set this value explicitly; it is in ``rule.overrides``.
``ZONE``
    the zone set it, and no rule overrode it.
``DEFAULT``
    nobody set it. The value is the one the schema carries for a field nobody
    has an opinion about, which is the case an operator most often mistakes for
    a setting they made.

The distinction between ``ZONE`` and ``DEFAULT`` is drawn by comparing against
the field's declared default rather than by remembering which fields were named
in the last patch. That is deliberate: an operator who explicitly sets caching
to the value it already had has changed nothing an edge can observe, and an
explanation that claimed otherwise would be describing the audit log rather
than the configuration.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.dns.domain import Domain, Rule
from blitzecdn.capabilities.serving.policy import SitePolicy

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
    #: The rule that decided it, when ``origin`` is ``RULE``. Never set
    #: otherwise: a zone value that a rule *could* have overridden and did not
    #: is a zone value, and naming the rule beside it reads as though the rule
    #: chose to leave it alone — which is a decision no rule records.
    rule: str | None = None


class HostExplanation(BaseModel):
    """Every effective setting for one derived host, and its source.

    Keyed by the derived host rather than by hostname. A group of hostnames
    that resolve alike *is* one virtual host — same zone, same winning rule,
    same origin — so one explanation covers all of them, and emitting one per
    hostname would be the same document repeated with the ``server_names`` list
    sliced up.
    """

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
    """Each policy field's schema default, in JSON form.

    Built from the composed policy's flat editing projection. Identity is not
    a policy component, and nested storage containers are not operator setting
    names; the explanation must use the same vocabulary as the zone editor.
    """
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

    Takes the zone and the winning rule rather than the merged policy, because
    the merge is exactly the information being recovered: once the two
    documents are one, ``cache_enabled=False`` no longer says whether the zone
    or the rule wanted it that way.
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
