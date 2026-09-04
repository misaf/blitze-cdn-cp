"""Alembic environment for the control-plane database.

The database URL comes from :class:`blitzecdn.core.config.Settings`, not from
``alembic.ini``. An installed controller keeps its state wherever
``blitzecdn.toml`` or ``BLITZE_DATABASE_PATH`` says, and a migration that ran
against a path in a config file nobody edits would upgrade the wrong file — or
create an empty one and report success.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from blitzecdn.core.config import Settings
from blitzecdn.core.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    """Where this installation actually keeps its database.

    Most specific first: an explicit ``-x url=...`` for restoring or inspecting
    a copy, then a URL set on the config by a caller driving Alembic in-process,
    then what this installation is actually configured to use.
    """
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
        # SQLite cannot ALTER most things in place, so Alembic rewrites the
        # table and copies the rows. Without this, every column change in this
        # project's future is a hand-written migration.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
