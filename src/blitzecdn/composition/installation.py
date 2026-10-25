"""Maintenance wiring that can run before an application exists."""

from blitzecdn.core.config import Settings
from blitzecdn.core.persistence.schema import DatabaseSchema


def migrate_database(settings: Settings) -> None:
    """Prepare persistence without constructing services or runtime features."""
    DatabaseSchema.migrate(settings.database_path)
