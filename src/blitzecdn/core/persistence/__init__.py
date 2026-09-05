"""The one SQLite database: its engine, its schema, and the stores on it.

`engine` owns the connection, the write lock and the Unit of Work; `tables`
carries the base every table in the database is declared on, and core's own
three rows; `schema` answers which revision a file is stamped with. The two
modules below it are persistence for concerns no single capability owns — the
audit log and the configuration tables.

What is deliberately *not* here is the bundle. `Repository` puts a capability
store on this engine for each slice, and choosing that set is composition, so
it lives in `blitzecdn.composition.repository`. Core supplies the database; it
does not decide who is on it.

Private by construction. An entry layer reaches a store through the port its
service declared, never through this package, and an installed distribution may
name exactly one module in here: `persistence.schema`, because a backup records
the revision it was taken at. Both rules are held in `tests/architecture`.
"""
