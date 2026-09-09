"""Shared source and manifest inspection helpers."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from paths import REPO_ROOT, SOURCE, optional_packages

from blitzecdn.core.plugins import (
    ENTRY_POINT_GROUP,
)


#: The distribution name a workspace member declares, and the import package it
#: ships, are derived from the directory rather than listed: `blitzecdn-cache`
#: ships `blitzecdn_cache`. Adding a package therefore needs no edit here.
def _distribution(package: Path) -> str:
    return package.name


def _next_major_bound() -> str:
    """The upper bound a workspace dependency carries, from the current version.

    Written as `<4` on both sides of this file until the 4.0.0 release, which
    is a version literal inside a rule — it passes for one major line and then
    fails at exactly the moment a release is being prepared, on a test whose
    subject is not versions. Derived from the root manifest instead: the cap is
    the major after the one this workspace is on, whatever that is.
    """
    version = _manifest(REPO_ROOT)["project"]["version"]
    return f"<{int(version.split('.')[0]) + 1}"


def _import_package(package: Path) -> str:
    return package.name.replace("-", "_")


def _manifest(package: Path) -> dict:
    return tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))


def _source_files(package: Path) -> list[Path]:
    return sorted((package / "src").rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _packages() -> list[Path]:
    found = optional_packages()
    assert found, "the workspace declares packages/* but ships none"
    return found


def _optional_import_roots() -> set[str]:
    return {_import_package(package) for package in _packages()}


def _built_in_capabilities() -> list[Path]:
    root = SOURCE / "capabilities"
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    )


#: A capability that decides something has a `service/`, an `api/`, or both.
#: The contract-only ones — `cache`, `compression`, `http`, `security`, `tls` —
#: are a set of pydantic models composed into `SitePolicy`, and they are tested
#: where they compose, in `tests/capabilities/dns/test_policy.py` and in
#: `tests/contract/`. Requiring a directory for those would buy a file that
#: restates the composition test from one capability's side.
_BEHAVIOUR_LAYERS = ("service", "api")


# --- GeoIP is optional; the settings that ask for a country are not ---------


#: The three stable fields that ask the edge which country a visitor is in.
#: Two belong to `SecurityPolicy` and one to the site's own header policy —
#: different owners, one capability behind them, which is why there is one
#: wheel rather than one per consumer.
_COUNTRY_SETTINGS = ("allowed_countries", "denied_countries", "ip_country")


def _entry_point_names(package: Path) -> set[str]:
    document = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
    group = document.get("project", {}).get("entry-points", {})
    return set(group.get(ENTRY_POINT_GROUP, {}))
