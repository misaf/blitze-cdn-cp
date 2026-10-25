"""The rule editor: every write checks the zone exists and is audited."""

from __future__ import annotations

from blitzecdn.capabilities.dns.domain import (
    ResolvedPolicy,
    Rule,
    RulePatch,
    reject_issuer_owned_certificate,
    resolve_policy,
)
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    RuleStore,
    UnitOfWork,
    ZoneReader,
)
from blitzecdn.core.domain.events import domain_event

__all__ = ["RuleService"]


class RuleService:
    """The editor for the overrides that apply to some hostnames and not others."""

    def __init__(
        self,
        *,
        rules: RuleStore,
        zones: ZoneReader,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.rules = rules
        self.zones = zones
        self.events = events
        self.uow = uow

    def list_rules(self, domain: str | None = None) -> list[Rule]:
        if domain is not None:
            self.zones.get_domain(domain)
        return self.rules.list_rules(domain)

    def get_rule(self, domain: str, name: str) -> Rule:
        return self.rules.get_rule(domain, name)

    def resolve(self, domain: str, fqdn: str) -> ResolvedPolicy:
        """How one hostname is served, and which rule decided it."""
        return resolve_policy(
            self.zones.get_domain(domain), self.rules.list_rules(domain), fqdn
        )

    def create_rule(self, rule: Rule, operator: str) -> Rule:
        self.zones.get_domain(rule.domain)
        reject_issuer_owned_certificate(rule.overrides)
        with self.uow.transaction():
            created = self.rules.create_rule(rule)
            self.events.record(
                domain_event(
                    operator,
                    "rule.created",
                    "rule",
                    f"{rule.domain}/{rule.name}",
                    {"match": rule.match, "overrides": sorted(rule.overrides)},
                )
            )
        return created

    def update_rule(
        self, domain: str, name: str, patch: RulePatch, operator: str
    ) -> Rule:
        """Merge a partial change into a rule; ``overrides`` replaces wholesale."""
        changes = patch.model_dump(exclude_unset=True)
        # Asked of the incoming overrides only, so editing an issuer-written rule's
        # ``match`` is not refused for a mode the operator did not send.
        if "overrides" in changes:
            reject_issuer_owned_certificate(changes["overrides"])
        with self.uow.transaction():
            # Read and written under one boundary, since the issuer writes rules too.
            current = self.rules.get_rule(domain, name)
            updated = Rule.model_validate({**current.model_dump(), **changes})
            saved = self.rules.replace_rule(updated, expected=current)
            self.events.record(
                domain_event(
                    operator,
                    "rule.updated",
                    "rule",
                    f"{domain}/{name}",
                    {"fields": sorted(changes)},
                )
            )
        return saved

    def delete_rule(self, domain: str, name: str, operator: str) -> None:
        with self.uow.transaction():
            self.rules.delete_rule(domain, name)
            self.events.record(
                domain_event(operator, "rule.deleted", "rule", f"{domain}/{name}")
            )
