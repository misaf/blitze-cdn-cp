"""Persistence for the per-hostname overrides on a zone's policy.

Its own module beside the zone store rather than inside it: the rules are their
own table with their own ordering, and a store that held both would be the one
place every zone read and every rule read had to go through.
"""

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import col

from blitzecdn.capabilities.dns.adapters.tables import RuleRow
from blitzecdn.capabilities.dns.domain import Rule
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.engine import Database


class RuleStore:
    """The overrides, ordered the way the resolver reads them."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_rules(self, domain: str | None = None) -> list[Rule]:
        """Every rule, in the order a hostname is matched against them.

        Ordered here rather than by the caller because "first match wins" is
        only meaningful against one order, and the store is the one place every
        reader goes through.
        """
        query = select(RuleRow).order_by(
            col(RuleRow.domain), col(RuleRow.priority), col(RuleRow.name)
        )
        if domain is not None:
            query = query.where(col(RuleRow.domain) == domain)
        with self._db.session() as session:
            return [self._rule(row) for row in session.scalars(query).all()]

    def get_rule(self, domain: str, name: str) -> Rule:
        with self._db.session() as session:
            row = session.get(RuleRow, (domain, name))
            if row is None:
                raise NotFoundError(f"rule {name!r} in {domain!r} does not exist")
            return self._rule(row)

    def create_rule(self, rule: Rule) -> Rule:
        with self._db.session() as session:
            session.add(self._row(rule))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    f"rule {rule.name!r} already exists in {rule.domain!r}"
                ) from exc
        return rule

    def replace_rule(self, rule: Rule) -> Rule:
        with self._db.session() as session:
            row = session.get(RuleRow, (rule.domain, rule.name))
            if row is None:
                raise NotFoundError(
                    f"rule {rule.name!r} in {rule.domain!r} does not exist"
                )
            self._apply(row, rule)
        return rule

    def delete_rule(self, domain: str, name: str) -> None:
        with self._db.session() as session:
            row = session.get(RuleRow, (domain, name))
            if row is None:
                raise NotFoundError(f"rule {name!r} in {domain!r} does not exist")
            session.delete(row)

    def replace_all_rules(self, rules: list[Rule]) -> None:
        """Restore the table wholesale. Used by rollback and backup restore."""
        with self._db.session() as session:
            session.execute(delete(RuleRow))
            session.flush()
            session.add_all([self._row(rule) for rule in rules])

    def _row(self, rule: Rule) -> RuleRow:
        row = RuleRow(domain=rule.domain, name=rule.name)
        self._apply(row, rule)
        return row

    def _apply(self, row: RuleRow, rule: Rule) -> None:
        row.priority = rule.priority
        row.match = rule.match
        row.overrides = dict(rule.overrides)
        row.enabled = rule.enabled
        row.updated_at = self._db.now()

    @staticmethod
    def _rule(row: RuleRow) -> Rule:
        return Rule.model_validate(
            {
                "domain": row.domain,
                "name": row.name,
                "priority": row.priority,
                "match": row.match,
                "overrides": row.overrides,
                "enabled": row.enabled,
            }
        )


__all__ = ["RuleStore"]
