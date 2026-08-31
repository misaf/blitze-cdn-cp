"""Persistence for the edge fleet."""

# mypy: disable-error-code="attr-defined,arg-type,call-overload,union-attr"

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from blitzecdn.core.database_engine import Database
from blitzecdn.core.database_models import EdgeRow
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.features.edges.domain import Edge


class EdgeStore:
    """The fleet, and the table the Ansible inventory plugin reads."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_edges(self) -> list[Edge]:
        with self._db.session() as session:
            rows = session.scalars(select(EdgeRow).order_by(EdgeRow.name)).all()
            return [self._edge(row) for row in rows]

    def get_edge(self, name: str) -> Edge:
        with self._db.session() as session:
            row = session.get(EdgeRow, name)
            if row is None:
                raise NotFoundError(f"edge {name!r} does not exist")
            return self._edge(row)

    def create_edge(self, edge: Edge) -> Edge:
        with self._db.session() as session:
            session.add(self._row(edge))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"edge {edge.name!r} already exists") from exc
        return edge

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge:
        if expected is not None:
            self._db.require_transaction("replace_edge(expected=...)")
        with self._db.session() as session:
            row = session.get(EdgeRow, edge.name)
            if row is None:
                raise NotFoundError(f"edge {edge.name!r} does not exist")
            if expected is not None and self._edge(row) != expected:
                raise ConflictError(
                    f"edge {edge.name!r} changed while it was being edited"
                )
            self._apply(row, edge)
        return edge

    def delete_edge(self, name: str) -> None:
        with self._db.session() as session:
            row = session.get(EdgeRow, name)
            if row is None:
                raise NotFoundError(f"edge {name!r} does not exist")
            session.delete(row)

    def _row(self, edge: Edge) -> EdgeRow:
        row = EdgeRow(name=edge.name)
        self._apply(row, edge)
        return row

    def _apply(self, row: EdgeRow, edge: Edge) -> None:
        row.host = edge.host
        row.user = edge.user
        row.port = edge.port
        row.private_key_file = edge.private_key_file
        row.public_addresses = list(edge.public_addresses)
        row.ssh_sources = list(edge.ssh_sources)
        row.updated_at = self._db.now()

    @staticmethod
    def _edge(row: EdgeRow) -> Edge:
        return Edge.model_validate(
            {
                "name": row.name,
                "host": row.host,
                "user": row.user,
                "port": row.port,
                "private_key_file": row.private_key_file,
                "public_addresses": tuple(row.public_addresses),
                "ssh_sources": tuple(row.ssh_sources),
            }
        )


__all__ = ["EdgeStore"]
