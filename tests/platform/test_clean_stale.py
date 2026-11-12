"""`scripts/clean-stale.sh` removes stale bytecode and nothing else.

Each case copies the script into a throwaway git repository shaped like this
checkout, so what it deletes can be checked against a tree built for the case
rather than against the real one.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from support.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "clean-stale.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argument list
        ["git", "-c", "init.defaultBranch=main", *args],  # noqa: S607 - git from PATH
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _touch(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _invoke(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the copied script, fixed flags
        ["bash", str(repo / "scripts" / "clean-stale.sh"), *args],  # noqa: S607 - bash from PATH
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _run(repo: Path, *args: str) -> str:
    result = _invoke(repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "commit.gpgsign", "false")
    _touch(root / ".gitignore", "__pycache__/\n*.pyc\n.env\n.venv/\n.state/\n")
    (root / "scripts").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "clean-stale.sh")
    _touch(root / "src" / "pkg" / "__init__.py")
    _touch(root / "src" / "pkg" / "sites" / "__init__.py")
    _touch(root / "src" / "pkg" / "kept" / ".gitkeep")
    _touch(root / "tests" / "test_x.py")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "seed")
    return root


def _stale_package(repo: Path) -> Path:
    """A package whose source is gone and whose bytecode is not."""
    dns = repo / "src" / "pkg" / "dns"
    _touch(dns / "__pycache__" / "service.cpython-313.pyc", "x")
    _touch(dns / "adapters" / "__pycache__" / "nginx.cpython-313.pyc", "x")
    return dns


def test_a_cache_only_package_is_removed_entirely(repo: Path) -> None:
    dns = _stale_package(repo)

    _run(repo)

    assert not dns.exists()
    assert (repo / "src" / "pkg" / "sites" / "__init__.py").exists()


def test_caches_beside_live_source_go_and_the_source_stays(repo: Path) -> None:
    cache = _touch(repo / "src" / "pkg" / "sites" / "__pycache__" / "a.pyc", "x")
    stray = _touch(repo / "tests" / "test_x.pyc", "x")

    _run(repo)

    assert not cache.parent.exists()
    assert not stray.exists()
    assert (repo / "src" / "pkg" / "sites" / "__init__.py").exists()
    assert (repo / "tests" / "test_x.py").exists()


def test_tracked_files_and_intentional_empty_directories_are_kept(repo: Path) -> None:
    empty = repo / "src" / "pkg" / "placeholder"
    empty.mkdir()
    tracked = _touch(
        repo / "src" / "pkg" / "vendored" / "__pycache__" / "keep.pyc", "x"
    )
    _git(repo, "add", "--force", str(tracked))
    # A sibling of a stale cache, already empty: the walk upwards stops at its
    # parent, which still holds it.
    _touch(repo / "src" / "pkg" / "old" / "__pycache__" / "m.pyc", "x")
    (repo / "src" / "pkg" / "old" / "fixtures").mkdir()

    _run(repo)

    assert empty.is_dir()
    assert tracked.exists()
    assert (repo / "src" / "pkg" / "kept" / ".gitkeep").exists()
    assert (repo / "src" / "pkg" / "old" / "fixtures").is_dir()
    assert not (repo / "src" / "pkg" / "old" / "__pycache__").exists()


def test_operational_data_is_never_touched(repo: Path) -> None:
    outside = [
        _touch(repo / ".env", "SECRET=1"),
        _touch(repo / ".venv" / "lib" / "__pycache__" / "site.pyc", "x"),
        _touch(repo / ".state" / "__pycache__" / "x.pyc", "x"),
        _touch(repo / "src" / "pkg" / ".state" / "__pycache__" / "x.pyc", "x"),
        _touch(repo / "src" / "nested" / ".git", "gitdir: elsewhere"),
        _touch(repo / "src" / "nested" / "__pycache__" / "y.pyc", "x"),
    ]

    _run(repo)

    for path in outside:
        assert path.exists(), path


def test_a_dry_run_reports_and_removes_nothing(repo: Path) -> None:
    dns = _stale_package(repo)

    report = _run(repo, "--dry-run")

    assert dns.exists()
    assert "would remove empty src/pkg/dns/adapters/" in report
    assert "would remove empty src/pkg/dns/" in report
    assert "src/pkg/sites" not in report


def test_a_second_run_finds_nothing(repo: Path) -> None:
    _stale_package(repo)
    _run(repo)

    assert "no stale Python caches" in _run(repo)


def test_hooks_install_run_on_checkout_and_uninstall(repo: Path) -> None:
    _run(repo, "--install-hooks")
    hooks = repo / ".git" / "hooks"
    assert (hooks / "post-merge").exists()
    assert (hooks / "post-checkout").exists()

    dns = _stale_package(repo)
    _git(repo, "checkout", "--quiet", "-b", "other")
    assert not dns.exists()

    # Idempotent: its own hooks are rewritten, not refused.
    _run(repo, "--install-hooks")
    _run(repo, "--uninstall-hooks")
    assert not (hooks / "post-merge").exists()
    assert not (hooks / "post-checkout").exists()


def test_installing_refuses_to_replace_a_foreign_hook(repo: Path) -> None:
    foreign = _touch(
        repo / ".git" / "hooks" / "post-checkout", "#!/bin/sh\necho mine\n"
    )

    result = _invoke(repo, "--install-hooks")

    assert result.returncode == 1
    assert foreign.read_text() == "#!/bin/sh\necho mine\n"
    assert not (repo / ".git" / "hooks" / "post-merge").exists()
