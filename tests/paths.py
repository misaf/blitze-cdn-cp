"""Where the repository is, asked once.

Every suite that reads a role, a template, a migration or the installer used to
count ``..`` from its own file. That made the number a fact about which
directory a test happened to sit in, so moving a test into its capability's
directory silently pointed it at ``tests/ansible/roles``. One anchor instead:
this module sits at the top of ``tests/``, and nothing below it counts.
"""

from pathlib import Path

#: The checkout root — the directory holding `src/` and `pyproject.toml`.
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
CORE_ANSIBLE = SOURCE / "assets" / "ansible"

#: The image build inputs, which live inside the package for exactly the
#: reason the Ansible above does: an installed controller has no checkout to
#: find them in. `blitzecdn.assets.docker` is the authority on these paths and the
#: suites read their constants from it; this anchor exists so a test can ask
#: the *checkout* question — is the tree where the packaging expects it — and
#: `test_the_published_docker_paths_are_the_ones_in_the_checkout` asserts the
#: two agree.
CORE_DOCKER = SOURCE / "assets" / "docker"

__all__ = [
    "CORE_ANSIBLE",
    "CORE_DOCKER",
    "FIXTURES",
    "REPO_ROOT",
    "SOURCE",
    "TESTS_DIR",
]
