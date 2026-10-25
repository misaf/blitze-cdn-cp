"""The one SQLite database: its engine, its schema, and core's own stores.

`engine` owns the connection, write lock and Unit of Work; `tables` the
declarative base and core's tables; `schema` the stamped revision; `audit` and
`configuration` the stores no capability owns. Which capability stores sit on
the engine is composition, in `blitzecdn.composition.repository`. Private: a
capability may name only `persistence.schema`.
"""
