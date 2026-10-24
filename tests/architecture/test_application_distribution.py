"""Application assembly is explicit; runtime providers own infrastructure only."""

import ast
import importlib
import importlib.metadata
import tomllib
from pathlib import Path

from paths import REPO_ROOT, SOURCE

from blitzecdn.composition import BUILTIN_PLUGINS, load_control_plane_plugins
from blitzecdn.core.plugins.resolution import resolve_nginx_resources


def test_one_distribution_owns_the_application_and_all_internal_providers():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert manifest["project"]["name"] == "blitzecdn"
    assert "optional-dependencies" not in manifest["project"]
    assert "workspace" not in manifest.get("tool", {}).get("uv", {})
    assert not list((REPO_ROOT / "packages").glob("*/pyproject.toml"))
    requirements = importlib.metadata.requires("blitzecdn") or ()
    assert not any(requirement.startswith("blitzecdn-") for requirement in requirements)


def test_all_shipped_providers_are_required_and_share_the_application_version():
    registry = load_control_plane_plugins(entry_point_group=None)
    version = importlib.metadata.version("blitzecdn")
    assert len(registry.plugins) == len(BUILTIN_PLUGINS)
    assert len(set(BUILTIN_PLUGINS)) == len(BUILTIN_PLUGINS)
    assert all(
        provider.required and provider.version == version
        for provider in registry.plugins
    )
    assert {
        "backup",
        "cache",
        "certificates",
        "origins",
        "compression",
        "geoip",
        "hardening",
        "http3",
        "resolver",
        "security",
    } <= registry.capabilities
    assert registry.rejected == ()
    for module in BUILTIN_PLUGINS:
        assert Path(importlib.import_module(module).__file__).is_relative_to(SOURCE)


def test_no_application_module_imports_a_retired_distribution_namespace():
    offenders = []
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            offenders.extend(
                f"{path.relative_to(SOURCE)}: {name}"
                for name in names
                if name.startswith("blitzecdn_")
            )
    assert offenders == []


def test_focused_runtime_providers_have_no_business_services_or_delivery_layers():
    for provider in (SOURCE / "providers").iterdir():
        if not provider.is_dir() or provider.name == "__pycache__":
            continue
        assert (provider / "provider.py").is_file()
        assert not any(
            (provider / layer).exists() for layer in ("service", "domain", "api", "cli")
        )
        for path in provider.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert (
                        ".service" not in node.module and ".adapters" not in node.module
                    )


def test_every_declared_role_and_nginx_fragment_is_owned_by_the_application():
    registry = load_control_plane_plugins(entry_point_group=None)
    owners = set()
    for contribution in registry.ansible_contributions():
        assert contribution.roles_path.is_relative_to(SOURCE)
        for role in (
            *contribution.edge_roles,
            *contribution.host_roles,
            *contribution.teardown_roles,
        ):
            assert role not in owners, f"duplicate role: {role}"
            owners.add(role)
            assert (contribution.roles_path / role / "tasks/main.yml").is_file()
    assert owners
    resources = resolve_nginx_resources(registry.nginx_contributions())
    assert all(
        resource.template.is_relative_to(SOURCE) and resource.template.is_file()
        for group in resources.values()
        for resource in group
    )
