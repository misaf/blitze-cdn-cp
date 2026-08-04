"""Verify what the control plane emits against what the edge roles declare.

These two halves ship as separate repositories, so nothing else stops a new
`CdnSite` field from reaching an edge role that has never heard of it. Every
assertion here is about the boundary between them, not about either side alone:

* the desired-state document carries a schema version the role supports;
* every key `CdnSite.to_ansible()` emits is declared in `argument_specs.yml`;
* every key the role marks required is actually present;
* declared `choices` cover the values the domain enums can produce;
* `site.conf.j2` renders from real model output without raising.

When one of these fails, the fix is to change both repositories together and
bump `DESIRED_STATE_VERSION` if older roles cannot honour the new shape.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import (
    DESIRED_STATE_VERSION,
    CdnSite,
    CertificateMode,
    OriginScheme,
)
from blitzecdn.infrastructure.database import Repository

jinja2 = pytest.importorskip("jinja2")

ROLE_DIR = Path(__file__).resolve().parent.parent / "ansible/roles/blitzecdn_nginx"
FIXTURE = Path(__file__).resolve().parent / "fixtures/desired-state.yml"


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


def _role_defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE_DIR / "defaults/main.yml").read_text(encoding="utf-8"))


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    """Render desired state the way a real deployment would."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository)
    repository.create_site(
        CdnSite.model_validate(
            {
                "name": "example-cdn",
                "server_names": ["cdn.example.com", "*.assets.example.com"],
                "origin_host": "origin.example.com",
                "origin_port": 8443,
                "origin_scheme": OriginScheme.HTTPS,
                "origin_request_host": "origin.example.com",
                "origin_sni": "origin.example.com",
                "cache_enabled": True,
                "cache_valid_success": "10m",
                "cache_valid_not_found": "1m",
            }
        )
    )
    repository.create_site(
        CdnSite.model_validate(
            {
                "name": "plain-cdn",
                "server_names": ["static.example.com"],
                "origin_host": "192.0.2.10",
                "origin_scheme": OriginScheme.HTTP,
                "enabled": False,
                "cache_enabled": False,
                "certificate_mode": CertificateMode.EXISTING,
                "certificate_path": "/etc/ssl/plain/fullchain.pem",
                "certificate_key_path": "/etc/ssl/plain/privkey.pem",
            }
        )
    )
    control._write_desired_state(repository.snapshot())
    return yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))


def test_desired_state_declares_a_version_the_role_supports(desired_state):
    supported = _role_defaults()["blitzecdn_nginx_supported_state_versions"]
    assert desired_state["blitzecdn_desired_state_version"] == DESIRED_STATE_VERSION
    assert DESIRED_STATE_VERSION in supported, (
        "The control plane emits a schema version the edge role rejects. "
        "Ship both repositories together."
    )


def test_every_emitted_key_is_declared_by_the_role(desired_state):
    declared = set(_role_spec()["blitzecdn_nginx_sites"]["options"])
    # The control plane adds these when distributing managed certificates.
    declared |= {"certificate_source_path", "certificate_key_source_path"}
    for site in desired_state["blitzecdn_nginx_sites"]:
        undeclared = set(site) - declared
        assert not undeclared, (
            f"CdnSite emits {sorted(undeclared)}, which "
            "ansible/roles/blitzecdn_nginx/meta/argument_specs.yml does not "
            "declare. Add them there and bump DESIRED_STATE_VERSION if older "
            "roles cannot ignore them."
        )


def test_required_keys_are_always_emitted(desired_state):
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    required = {name for name, spec in options.items() if (spec or {}).get("required")}
    for site in desired_state["blitzecdn_nginx_sites"]:
        missing = required - set(site)
        assert not missing, (
            f"site {site.get('name')!r} omits required {sorted(missing)}"
        )


@pytest.mark.parametrize(
    ("field", "enum"),
    [("origin_scheme", OriginScheme), ("certificate_mode", CertificateMode)],
)
def test_role_choices_cover_every_domain_value(field, enum):
    """A new enum member must not reach a role that rejects it."""
    declared = set(_role_spec()["blitzecdn_nginx_sites"]["options"][field]["choices"])
    assert {member.value for member in enum} <= declared, (
        f"{enum.__name__} has values the role's {field} choices do not allow"
    )


def test_site_template_renders_from_real_model_output(desired_state):
    """Catches template breakage that --syntax-check cannot see."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    context = _role_defaults()
    for site in desired_state["blitzecdn_nginx_sites"]:
        rendered = environment.get_template("site.conf.j2").render(**context, item=site)
        assert f"server_name {' '.join(site['server_names'])};" in rendered
        assert "proxy_pass" in rendered
        if site["certificate_mode"] == CertificateMode.DISABLED:
            assert "ssl_certificate" not in rendered
        else:
            assert f"ssl_certificate {site['certificate_path']};" in rendered


def test_committed_fixture_matches_generated_desired_state(desired_state):
    """CI feeds this fixture to a real playbook; keep it honest.

    Set BLITZECDN_UPDATE_FIXTURE=1 to rewrite it after an intentional change.
    """
    if os.environ.get("BLITZECDN_UPDATE_FIXTURE"):
        FIXTURE.write_text(
            "---\n" + yaml.dump(desired_state, Dumper=_IndentedDumper, sort_keys=False),
            encoding="utf-8",
        )
    committed = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert committed == desired_state, (
        f"{FIXTURE} is stale. Regenerate it with:\n"
        "  BLITZECDN_UPDATE_FIXTURE=1 .venv/bin/python -m pytest "
        "tests/test_contract.py"
    )
