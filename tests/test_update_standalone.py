import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "update-standalone.sh"
BASH = "/bin/bash"


def test_updater_has_valid_shell_syntax():
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_updater_help_does_not_require_root():
    result = subprocess.run(  # noqa: S603 - fixed executable and repository script
        [BASH, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--version vX.Y.Z" in result.stdout
    assert "--check" in result.stdout
    assert "--yes" in result.stdout


def test_updater_uses_only_stable_release_tags_and_never_deploys():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "repo_git ls-remote --tags --refs origin 'v*'" in script
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in script
    assert '"${install_dir}/install.sh"' in script
    assert "/usr/local/bin/blitzecdn doctor" in script
    assert (
        re.search(r"(?m)^\s*(?:run_blitzecdn|/usr/local/bin/blitzecdn) deploy", script)
        is None
    )


def test_updater_backs_up_state_and_has_failure_rollback():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "backup_items=(opt/blitzecdn/.state)" in script
    assert "etc/blitzecdn" in script
    assert "rollback()" in script
    assert 'repo_git checkout --detach "${previous_commit}"' in script


def test_updater_scopes_git_safe_directory_without_changing_global_config():
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'git -c safe.directory="${install_dir}" "$@"' in script
    assert "git config --global" not in script
