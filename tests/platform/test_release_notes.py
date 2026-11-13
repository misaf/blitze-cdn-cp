"""`scripts/release_notes.py` turns a changelog section into a release's notes.

CONTRIBUTING publishes every GitHub release from this script, so it must find
every version the changelog describes and keep breaking changes first.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from support.paths import REPO_ROOT


def _load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module by name while the class is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_notes = _load()

CHANGELOG = """\
# Changelog

## [Unreleased]

## [2.0.0] - 2026-01-02

2.0.0 needs a fresh
install.

### Added

- A new
  command (#3).

### Changed

- **BREAKING:** The API
  moved (#4).
- Faster startup.

### Removed

- **BREAKING:** `old` (#5).

## [1.0.0] - 2026-01-01

### Fixed

- A crash.

[Unreleased]: https://example.test/compare/v2.0.0...HEAD
[2.0.0]: https://example.test/compare/v1.0.0...v2.0.0
[1.0.0]: https://example.test/releases/tag/v1.0.0
"""


def test_breaking_entries_come_first_and_lines_are_joined() -> None:
    assert release_notes.notes(CHANGELOG, "2.0.0") == (
        "2.0.0 needs a fresh install.\n\n"
        "### Breaking changes\n\n"
        "- The API moved (#4).\n"
        "- Removed: `old` (#5).\n\n"
        "### Added\n\n"
        "- A new command (#3).\n\n"
        "### Changed\n\n"
        "- Faster startup.\n\n"
        "**Full Changelog**: https://example.test/compare/v1.0.0...v2.0.0\n"
    )


def test_the_last_section_stops_before_the_link_references() -> None:
    assert release_notes.notes(CHANGELOG, "1.0.0") == (
        "### Fixed\n\n"
        "- A crash.\n\n"
        "**Full Changelog**: https://example.test/releases/tag/v1.0.0\n"
    )


def test_an_unknown_or_empty_version_is_refused() -> None:
    with pytest.raises(release_notes.NotesError, match=r"no section for 3\.0\.0"):
        release_notes.notes(CHANGELOG, "3.0.0")
    with pytest.raises(release_notes.NotesError, match=r"Unreleased section is empty"):
        release_notes.notes(CHANGELOG, "Unreleased")


def test_every_released_version_in_the_changelog_has_notes() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    versions = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]", changelog)
    assert versions
    for version in versions:
        text = release_notes.notes(changelog, version)
        assert "**BREAKING:**" not in text, version
        assert text.rstrip().endswith(f"v{version}"), f"{version} has no link"


def test_the_command_line_takes_a_v_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG)
    assert release_notes.main(["v1.0.0", "--changelog", str(path)]) == 0
    assert capsys.readouterr().out.startswith("### Fixed")
    assert release_notes.main(["1.0", "--changelog", str(path)]) == 2


def test_a_github_alert_keeps_its_marker_on_its_own_line() -> None:
    changelog = (
        "## [1.0.0] - 2026-01-01\n\n"
        "> [!IMPORTANT]\n"
        "> **A fresh\n"
        "> install.** Really.\n\n"
        "> A plain\n"
        "> quote.\n\n"
        "### Fixed\n\n"
        "- A crash.\n"
    )
    assert release_notes.notes(changelog, "1.0.0") == (
        "> [!IMPORTANT]\n"
        "> **A fresh install.** Really.\n\n"
        "> A plain quote.\n\n"
        "### Fixed\n\n"
        "- A crash.\n"
    )
