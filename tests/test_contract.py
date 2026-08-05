"""Verify what the control plane emits against what the edge roles declare.

The edge roles live in the `blitzecdn.edge` collection, pinned in
`ansible/requirements.yml`. These tests read the *installed* collection rather
than a copy in this repository, so they check this control plane against the
exact edge version an operator would deploy. Nothing else stops a new `CdnSite`
field from reaching a role that has never heard of it.

Every assertion here is about the boundary between them, not either side alone:

* the desired-state document carries a schema version the role supports;
* every key `CdnSite.to_ansible()` emits is declared in `argument_specs.yml`;
* every key the role marks required is actually present;
* declared `choices` cover the values the domain enums can produce;
* `site.conf.j2` renders from real model output without raising.

When one of these fails, the fix is to change both repositories together and
bump `DESIRED_STATE_VERSION` if older roles cannot honour the new shape.

Install the collection first, or these tests skip:

    ansible-galaxy collection install -r ansible/requirements.yml
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from blitzecdn import EDGE_COLLECTION_VERSION
from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import (
    DESIRED_STATE_VERSION,
    CdnSite,
    CertificateMode,
    OriginScheme,
)
from blitzecdn.infrastructure.database import Repository

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures/desired-state.yml"

#: Where ansible-galaxy puts collections, mirroring collections_path in
#: ansible/ansible.cfg. The first hit wins, as it does for Ansible itself.
_COLLECTION_PATHS = (
    PROJECT_DIR / ".state/collections",
    Path.home() / ".ansible/collections",
    Path("/usr/share/ansible/collections"),
)


def _installed_role(name: str) -> Path:
    for root in _COLLECTION_PATHS:
        candidate = root / "ansible_collections/blitzecdn/edge/roles" / name
        if candidate.is_dir():
            return candidate
    pytest.skip(
        "the blitzecdn.edge collection is not installed; run "
        "`ansible-galaxy collection install -r ansible/requirements.yml`",
        allow_module_level=True,
    )


ROLE_DIR = _installed_role("blitzecdn_nginx")


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


def test_edge_pin_matches_the_published_constant():
    """`EDGE_COLLECTION_VERSION` is what downstream consumers see.

    The wheel does not ship `ansible/requirements.yml`, so the documentation
    site reads the constant instead. If the two disagree, the site documents
    roles this control plane does not deploy.
    """
    document = yaml.safe_load(
        (PROJECT_DIR / "ansible/requirements.yml").read_text(encoding="utf-8")
    )
    pinned = next(
        entry["version"]
        for entry in document["collections"]
        if entry["name"] == "blitzecdn.edge"
    )
    assert pinned.removeprefix("v") == EDGE_COLLECTION_VERSION, (
        f"ansible/requirements.yml pins blitzecdn.edge {pinned} but "
        f"EDGE_COLLECTION_VERSION is {EDGE_COLLECTION_VERSION}. Bump both."
    )


def test_installed_collection_is_the_pinned_one():
    """Guard against testing green against an unpinned local collection."""
    manifest = ROLE_DIR.parent.parent / "MANIFEST.json"
    if not manifest.is_file():
        pytest.skip("collection installed from a source checkout; no MANIFEST.json")
    installed = json.loads(manifest.read_text(encoding="utf-8"))["collection_info"][
        "version"
    ]
    assert installed == EDGE_COLLECTION_VERSION, (
        f"blitzecdn.edge {installed} is installed but this control plane pins "
        f"{EDGE_COLLECTION_VERSION}. Reinstall with "
        "`ansible-galaxy collection install -r ansible/requirements.yml`."
    )


def test_edge_collection_enforces_public_key_only_ssh():
    """The control plane reaches every edge over SSH and nothing else.

    `ansible/ansible.cfg` refuses to authenticate with anything but a key, and
    the pinned collection is what makes the hosts agree. If a future edge
    release relaxes this drop-in, deploys keep working — the controller still
    has its key — while every edge quietly starts accepting passwords again.
    Nothing else in either repository would notice.
    """
    role = _installed_role("blitzecdn_sshd")
    template = (role / "templates/sshd.conf.j2").read_text(encoding="utf-8")
    directives = {
        line.split()[0].lower(): line.split(maxsplit=1)[1].strip()
        for line in template.splitlines()
        if line and not line.startswith(("#", "{"))
    }
    for keyword, expected in (
        ("pubkeyauthentication", "yes"),
        ("authenticationmethods", "publickey"),
        ("passwordauthentication", "no"),
        ("kbdinteractiveauthentication", "no"),
        ("permitemptypasswords", "no"),
        ("hostbasedauthentication", "no"),
    ):
        assert directives.get(keyword) == expected, (
            f"blitzecdn.edge {EDGE_COLLECTION_VERSION} no longer sets "
            f"{keyword} {expected} in blitzecdn_sshd. Edges would accept "
            "something other than public keys."
        )


def test_edge_ssh_hardening_is_on_by_default():
    """Opting out is possible; arriving opted out by accident is not."""
    defaults = yaml.safe_load(
        (_installed_role("blitzecdn_sshd") / "defaults/main.yml").read_text(
            encoding="utf-8"
        )
    )
    assert defaults["blitzecdn_sshd_enabled"] is True
    assert defaults["blitzecdn_sshd_permit_root_login"] == "no"


def test_controller_refuses_password_authentication():
    """The other half of the contract: what this repository dials out with."""
    config = (PROJECT_DIR / "ansible/ansible.cfg").read_text(encoding="utf-8")
    for option in (
        "PreferredAuthentications=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "BatchMode=yes",
    ):
        assert option in config, (
            f"ansible/ansible.cfg no longer passes -o {option}, so a deploy "
            "could authenticate to an edge with a password."
        )
    assert "host_key_checking = True" in config


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
            f"CdnSite emits {sorted(undeclared)}, which the pinned "
            "blitzecdn.edge collection does not declare in "
            "roles/blitzecdn_nginx/meta/argument_specs.yml. Add them in the "
            "edge repository, release it, bump the pin in "
            "ansible/requirements.yml, and bump DESIRED_STATE_VERSION if "
            "older roles cannot ignore them."
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
