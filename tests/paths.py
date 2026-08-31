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

#: The workspace's optional distributions. Each directory here is one wheel
#: that can be installed beside the control plane or left out of it.
PACKAGES = REPO_ROOT / "packages"


def optional_packages() -> list[Path]:
    """Every optional distribution in the workspace, by directory.

    Discovered rather than listed. A test that named the packages would have to
    be edited to add one, which is precisely the "no manual registration"
    property the whole boundary exists to provide — including for the tests
    that enforce it.
    """
    return sorted(
        path for path in PACKAGES.iterdir() if (path / "pyproject.toml").is_file()
    )


__all__ = [
    "FIXTURES",
    "PACKAGES",
    "REPO_ROOT",
    "SOURCE",
    "TESTS_DIR",
    "optional_packages",
]
