"""The schema revision stamped on the installed controller's database.

Run by the control-plane role in the CLI service of the image it converges,
before it stops the services and again if the run fails, so a failed update can
tell whether the database is still the one the previous release left. Prints
the revision, or an empty line when no database or no stamp exists yet; it only
reads, and never migrates.
"""

from __future__ import annotations

from blitzecdn.core.config import Settings
from blitzecdn.core.persistence.schema import DatabaseSchema


def main() -> None:
    print(DatabaseSchema(Settings.from_environment()).current() or "")


if __name__ == "__main__":
    main()
