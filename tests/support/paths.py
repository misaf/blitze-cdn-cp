"""Where the repository is, asked once.

Suites anchor on this module rather than counting ``..`` from their own file,
so moving a test does not repoint it.
"""

from pathlib import Path

#: The checkout root — the directory holding `src/` and `pyproject.toml`.
TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent
SOURCE = REPO_ROOT / "src" / "blitzecdn"
FIXTURES = TESTS_DIR / "fixtures"

#: The platform's Ansible, which ships inside the package: roles, plays, the
#: inventory plugin and the non-secret defaults.
CORE_ANSIBLE = SOURCE / "assets" / "ansible"

#: The image build inputs, also inside the package. `blitzecdn.assets.docker` is
#: the authority; this anchor lets
#: `test_the_published_docker_paths_are_the_ones_in_the_checkout` check the two
#: agree.
CORE_DOCKER = SOURCE / "assets" / "docker"

__all__ = [
    "CORE_ANSIBLE",
    "CORE_DOCKER",
    "FIXTURES",
    "REPO_ROOT",
    "SOURCE",
    "TESTS_DIR",
]
