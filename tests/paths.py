"""Where the repository is, asked once.

Every suite that reads a role, a template, a migration or the installer used to
count ``..`` from its own file. That made the number a fact about which
directory a test happened to sit in, so moving a test into its capability's
directory silently pointed it at ``tests/ansible/roles``. One anchor instead:
this module sits at the top of ``tests/``, and nothing below it counts.
"""

from pathlib import Path

#: The checkout root — the directory holding `src/`, `ansible/` and `docker/`.
REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
SOURCE = REPO_ROOT / "src" / "blitzecdn"
FIXTURES = TESTS_DIR / "fixtures"

__all__ = ["FIXTURES", "REPO_ROOT", "SOURCE", "TESTS_DIR"]
