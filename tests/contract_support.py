"""Shared paths and loaders for Ansible contract tests."""

# ruff: noqa: F401 -- these names are deliberately re-exported to test modules

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from paths import CORE_ANSIBLE, FIXTURES, REPO_ROOT

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.core.database import Repository
from blitzecdn.features.dns.domain import DnsRecord, Domain
from blitzecdn.features.security.policy import SiteFirewall
from blitzecdn.features.sites.domain import CdnSite, SitePolicy
from blitzecdn.features.sites.policy import CacheQueryStringMode
from blitzecdn.features.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = REPO_ROOT
FIXTURE = FIXTURES / "desired-state.yml"


#: Core's roles. They ship with this control plane, so there is no install step
#: to get wrong and no reason for these tests to skip. That matters: they used
#: to read an installed collection and skipped silently when it was absent,
#: which turned a broken contract into a green run.
#:
#: An optional capability's roles are not here. They ship inside that
#: capability's wheel and its own tests read them from there, which is the same
#: path a deployment resolves them by.
ROLES_DIR = CORE_ANSIBLE / "roles"


def _role(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), f"{name} is missing from src/blitzecdn/ansible/roles/"
    return candidate


ROLE_DIR = _role("blitzecdn_nginx")

#: The role that owns the shared edge runtime contract. blitzecdn_nginx,
#: blitzecdn_edge_stack and blitzecdn_firewall all read `blitzecdn_edge_runtime`
#: from here and none of them reads the others, so every test that renders one
#: of those roles needs the contract resolved first.
RUNTIME_ROLE_DIR = _role("blitzecdn_edge")


def _ansible_jinja(**kwargs: Any) -> Any:
    """A Jinja environment with the handful of Ansible filters the edge uses.

    Rendering the real templates and resolving the real defaults is the point:
    a compose file asserted on as text cannot tell a mount from a comment, and
    a contract asserted on before its expressions are evaluated is a dict of
    Jinja source.
    """
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, **kwargs)
    environment.filters["dirname"] = os.path.dirname
    environment.filters["basename"] = os.path.basename
    environment.filters["regex_replace"] = lambda value, pattern, replacement="": (
        re.sub(pattern, replacement, value)
    )
    environment.filters["bool"] = lambda value: (
        value
        if isinstance(value, bool)
        else str(value).strip().lower() in {"true", "yes", "on", "1"}
    )
    # `lookup('env', ...)` is Ansible's, not Jinja's. The defaults that use it
    # are secrets read from the controller's environment and are none of these
    # tests' business.
    environment.globals["lookup"] = lambda *_args, **_kwargs: ""
    return environment


def _resolve(value: Any, context: dict[str, Any], environment: Any) -> Any:
    """Render every Jinja expression nested anywhere inside ``value``.

    Ansible evaluates a default lazily, wherever it is used, so a contract
    member written as an expression is a real value by the time a role reads
    it. These tests have to do the same or they assert on template source.
    """
    if isinstance(value, str):
        if "{{" not in value:
            return value
        rendered = environment.from_string(value).render(**context).strip()
        return {"True": True, "False": False}.get(rendered, rendered)
    if isinstance(value, dict):
        return {
            key: _resolve(item, context, environment) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve(item, context, environment) for item in value]
    return value


def _runtime_source() -> dict[str, Any]:
    return yaml.safe_load(
        (RUNTIME_ROLE_DIR / "defaults/main.yml").read_text(encoding="utf-8")
    )


#: The contract's flat inputs — the members that are not fixed runtime layout.
#: They are flat because desired state and `blitzecdn config set` reach Ansible
#: as top-level variables and neither can override one member of a dictionary.
RUNTIME_INPUTS = frozenset(_runtime_source()) - {"blitzecdn_edge_runtime"}


def _runtime_defaults(**inputs: Any) -> dict[str, Any]:
    """The contract as a role sees it: every expression already evaluated."""
    source = _runtime_source() | inputs
    environment = _ansible_jinja()
    return source | {
        "blitzecdn_edge_runtime": _resolve(
            source["blitzecdn_edge_runtime"], source, environment
        )
    }


def _split_runtime(overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate contract inputs from ordinary variable overrides.

    A contract input has to be applied before the contract is composed —
    setting `blitzecdn_edge_http3_enabled` after the fact would leave
    `blitzecdn_edge_runtime.listeners.http3` reading the default, which is
    precisely the two-copies bug the contract exists to remove.
    """
    inputs = {
        name: value for name, value in overrides.items() if name in RUNTIME_INPUTS
    }
    return inputs, {
        name: value for name, value in overrides.items() if name not in RUNTIME_INPUTS
    }


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


def _role_defaults(**runtime_inputs: Any) -> dict[str, Any]:
    """blitzecdn_nginx's defaults on top of the resolved runtime contract.

    Which is the variable namespace the role actually renders from: it declares
    `blitzecdn_edge_runtime` as a required option and reads the paths, the
    listener sets, the status endpoint and the GeoIP database from it.
    """
    context = _runtime_defaults(**runtime_inputs) | yaml.safe_load(
        (ROLE_DIR / "defaults/main.yml").read_text(encoding="utf-8")
    )
    # Resolved, because Ansible resolves: a default written as an expression
    # over the contract — the status file's path, the access log's — is a real
    # value by the time a template reads it, and a test comparing template
    # source against a literal proves nothing.
    environment = _ansible_jinja()
    for _ in range(len(context)):
        resolved = {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }
        resolved = _resolve(resolved, context, environment)
        if resolved == {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }:
            break
        context |= resolved
    return context


STACK_ROLE_DIR = _role("blitzecdn_edge_stack")
DOCKER_ROLE_DIR = _role("blitzecdn_docker")


def run_role_tasks(
    tasks_file: Path, variables: dict[str, Any], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Execute one role's task file against localhost and report what happened.

    The only way these assertions are ever *evaluated* rather than merely
    parsed. `--syntax-check` and ansible-lint both accept a `when:` that raises
    at run time, and a conditional whose result is a dict rather than a boolean
    is exactly the shape that has shipped a broken deploy before.

    Shared here because a capability's role validates its own settings now, in
    its own package's tests, and every one of them needs the same three lines
    of playbook around a task file.
    """
    executable = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(executable).exists():
        pytest.skip("ansible-playbook is not installed")
    (tmp_path / "ansible-local").mkdir(exist_ok=True)
    playbook = tmp_path / f"{tasks_file.stem}-run.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [{"import_tasks": str(tasks_file)}],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [executable, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env=os.environ
        | {
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-local"),
            "ANSIBLE_ROLES_PATH": str(ROLES_DIR),
            # A capability's role uses the same collections core's roles do —
            # `community.docker` runs its updater container. Without this the
            # play fails to resolve the module, which looks like a broken role
            # rather than a test harness that never installed anything.
            "ANSIBLE_COLLECTIONS_PATH": str(PROJECT_DIR / ".state/collections"),
        },
    )


def _seed_site(repository, *, name, label, origin, **policy):
    """One site and the record that routes a hostname to it.

    Written through the stores rather than the services because these fixtures
    describe *state*, not the operations that produce it, and the desired-state
    document is what is under test. The hostname is stamped explicitly for the
    same reason: `dns` maintains that column and no service is involved here.
    """
    repository.sites.create_site(
        CdnSite.model_validate({"name": name, "origin_host": origin, **policy})
    )
    record = DnsRecord(domain="example.com", name=label, site=name)
    repository.zones.create_record(record)
    repository.sites.set_server_names(name, (record.fqdn,))


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    _seed_site(
        repository,
        name="cdn-example-com",
        label="cdn",
        origin="198.51.100.20",
        **{
            "ssl_mode": SslMode.OFF,
            "origin_request_host": "origin.example.com",
            "origin_sni": "origin.example.com",
            "cache_enabled": True,
            "cache_valid_success": "10m",
            "cache_valid_not_found": "1m",
        },
    )
    _seed_site(
        repository,
        name="static-example-com",
        label="static",
        origin="192.0.2.10",
        **{
            "ssl_mode": SslMode.FLEXIBLE,
            "enabled": False,
            "cache_enabled": False,
            "certificate_mode": CertificateMode.EXISTING,
            "certificate_path": "/etc/ssl/plain/fullchain.pem",
            "certificate_key_path": "/etc/ssl/plain/privkey.pem",
            "firewall": {
                "allow_sources": ["203.0.113.9"],
                "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
                "denied_methods": ["DELETE", "TRACE"],
                "denied_paths": ["/admin", "/.git"],
            },
        },
    )
    control.deployments.write_desired_state(
        repository.snapshot(), settings.generated_vars_path
    )
    return yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))


__all__ = [name for name in globals() if not name.startswith("__")]
