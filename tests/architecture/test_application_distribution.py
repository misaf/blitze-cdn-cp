"""Application assembly is explicit; edge providers own infrastructure only."""

import ast
import importlib
import importlib.metadata
import tomllib
from pathlib import Path

from support.paths import REPO_ROOT, SOURCE

from blitzecdn.composition import BUILTIN_FEATURES, load_registry
from blitzecdn.core.features.resolution import resolve_nginx_resources


def test_one_distribution_owns_the_application_and_every_capability():
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert manifest["project"]["name"] == "blitzecdn"
    assert "optional-dependencies" not in manifest["project"]
    assert "workspace" not in manifest.get("tool", {}).get("uv", {})
    assert not list((REPO_ROOT / "packages").glob("*/pyproject.toml"))
    requirements = importlib.metadata.requires("blitzecdn") or ()
    assert not any(requirement.startswith("blitzecdn-") for requirement in requirements)


def test_all_shipped_features_share_the_application_version():
    registry = load_registry()
    version = importlib.metadata.version("blitzecdn")
    declared = [
        feature
        for module in BUILTIN_FEATURES
        for feature in importlib.import_module(module).FEATURES
    ]
    assert len(registry.metadata) == len(declared)
    assert len(set(BUILTIN_FEATURES)) == len(BUILTIN_FEATURES)
    assert all(provider.version == version for provider in registry.metadata)
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
    for module in BUILTIN_FEATURES:
        assert Path(importlib.import_module(module).__file__).is_relative_to(SOURCE)


#: The slices that realize an edge capability and nothing more. They sit with
#: the capabilities they serve; this is what keeps them from growing a service.
EDGE_PROVIDERS = (
    "capabilities/hardening",
    "capabilities/http/http3",
    "capabilities/resolver",
    "capabilities/security/geoip",
)


def test_edge_providers_have_no_business_services_or_delivery_layers():
    for name in EDGE_PROVIDERS:
        provider = SOURCE / name
        assert (provider / "feature.py").is_file()
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
    registry = load_registry()
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
