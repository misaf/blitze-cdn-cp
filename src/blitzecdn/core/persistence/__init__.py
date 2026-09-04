"""The one SQLite database: its engine, its schema, and the stores on it.

`engine` owns the connection, the write lock and the Unit of Work; `models` is
the only description of what is on disk and what Alembic generates migrations
against; `repository` bundles the capability stores that sit on it; `schema`
answers which revision a file is stamped with. The three modules below it are
persistence for concerns no single capability owns — the audit log, the
configuration tables, the workflow journal.

Private by construction. An entry layer reaches a store through the port its
service declared, never through this package, and an installed distribution may
name exactly one module in here: `persistence.schema`, because a backup records
the revision it was taken at. Both rules are held in `tests/architecture`.
"""
