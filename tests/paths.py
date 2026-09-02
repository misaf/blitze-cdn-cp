"""Where the repository is, asked once.

Every suite that reads a role, a template, a migration or the installer used to
count ``..`` from its own file. That made the number a fact about which
directory a test happened to sit in, so moving a test into its capability's
directory silently pointed it at ``tests/ansible/roles``. One anchor instead:
this module sits at the top of ``tests/``, and nothing below it counts.
"""

from pathlib import Path

#: The checkout root — the directory holding `src/` and `docker/`.
REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
SOURCE = REPO_ROOT / "src" / "blitzecdn"
FIXTURES = TESTS_DIR / "fixtures"

#: The platform's Ansible, which lives *inside* the package rather than beside
#: it: roles, plays, the dynamic inventory plugin and the shipped non-secret
#: defaults all ship in the `blitzecdn` wheel, the same way every optional
#: capability ships its own. Anchored here for the same reason the root is —
#: a suite that spelled the path would have to be edited by the next move, and
#: this one has already happened once.
CORE_ANSIBLE = SOURCE / "ansible"

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
    "CORE_ANSIBLE",
    "FIXTURES",
    "PACKAGES",
    "REPO_ROOT",
    "SOURCE",
    "TESTS_DIR",
    "optional_packages",
]
