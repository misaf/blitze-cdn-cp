"""Alembic environment; the database URL comes from ``Settings``."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

# Every table module, imported so its rows register on `Base.metadata`;
# `test_migrations.py` fails if one is missing.
from blitzecdn.capabilities.deployments.adapters import (
    tables as _deployments,  # noqa: F401
)
from blitzecdn.capabilities.dns.adapters import tables as _dns  # noqa: F401
from blitzecdn.capabilities.edges.adapters import tables as _edges  # noqa: F401
from blitzecdn.capabilities.jobs.adapters import tables as _jobs  # noqa: F401
from blitzecdn.capabilities.releases.adapters import (
    tables as _releases,  # noqa: F401
)
from blitzecdn.capabilities.workflows.adapters import (
    tables as _workflows,  # noqa: F401
)
from blitzecdn.core.config import Settings
from blitzecdn.core.persistence.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    """The database to migrate: ``-x url=``, a caller-set URL, or ``Settings``."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    return f"sqlite+pysqlite:///{Settings.from_environment().database_path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things, so Alembic rebuilds tables.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
