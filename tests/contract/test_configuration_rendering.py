"""The controller renders exactly the configuration tree the edge play does.

`EdgeRenderer` re-creates, in-process, what `blitzecdn_nginx` writes into a
release's candidate directory. This holds the two together the only honest
way: the real roles run against localhost in check mode with ``--diff``, which
renders every template without writing it, and the rendered files and links
are compared byte for byte with the controller's bundle.

Check mode names a destination only when it already exists, so every path the
renderer predicts is created empty under a temporary release root first. A
template the edge play renders that the renderer did not predict therefore
arrives without a destination and fails the comparison — parity is held in
both directions, not only for the files the renderer knows about.

The release documents come from a real control plane's compiler, so they are
the documents a deployment ships.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from certificate_support import certificate_control_plane
from contract_support import PROJECT_DIR
from control_plane_fixtures import seed_site, with_capability_settings
from paths import CORE_ANSIBLE

from blitzecdn.capabilities.dns.domain import DomainPatch, PatchPolicy
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.ansible.rendering import EdgeRenderer
from blitzecdn.core.domain.configuration import ConfigurationBundle

#: Capability edge roles whose tasks need Docker, systemd or a resolver; their
#: defaults still load, which is all the rendered tree depends on.
_HOST_ONLY_ROLES = {"blitzecdn_geoip", "blitzecdn_http3", "blitzecdn_resolver"}

_SECRET = "parity-test-signing-key-" + "x" * 32
_PLACEHOLDER = b"# parity placeholder; no rendering produces this line\n"


def _document(control: ControlPlane, tmp_path: Path) -> dict[str, Any]:
    release = control.releases.compile()
    assert release.servable, release.findings
    return {
        **release.execution_document(),
        # Both renderers are pointed at the same temporary host paths: the two
        # values an edge supplies that a test cannot write as shipped.
        "blitzecdn_config_release_root": str(tmp_path / "releases"),
        "blitzecdn_security_under_attack_key_file": str(
            tmp_path / "secrets" / "under-attack.conf"
        ),
    }


def _playbook(control: ControlPlane, path: Path) -> None:
    roles: list[dict[str, Any]] = []
    for name in control.edge_contributions.edge_roles:
        entry: dict[str, Any] = {"ansible.builtin.import_role": {"name": name}}
        if name in _HOST_ONLY_ROLES:
            entry["when"] = False
        roles.append(entry)
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "blitzecdn_edges",
                    "gather_facts": False,
                    "vars": {"blitzecdn_nginx_stack_ready": True},
                    "pre_tasks": [
                        {"ansible.builtin.import_role": {"name": "blitzecdn_edge"}},
                        {
                            "ansible.builtin.import_role": {
                                "name": "blitzecdn_config_release",
                                "tasks_from": "select.yml",
                            }
                        },
                    ],
                    "tasks": [
                        *roles,
                        {"ansible.builtin.import_role": {"name": "blitzecdn_nginx"}},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )


def _edge_rendering(
    control: ControlPlane,
    document: dict[str, Any],
    bundle: ConfigurationBundle,
    tmp_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """What the edge play renders into the candidate, by relative path."""
    executable = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(executable).exists():
        pytest.skip("ansible-playbook is not installed")
    candidate = (
        Path(document["blitzecdn_config_release_root"])
        / (document["blitzecdn_release_digest"])
    )
    # A placeholder no rendering produces, so every predicted file is a
    # pending change: an unchanged file would make Ansible apply its root
    # ownership instead, which a test running as a user cannot do.
    for name in bundle.files:
        (candidate / name).parent.mkdir(parents=True, exist_ok=True)
        (candidate / name).write_bytes(_PLACEHOLDER)
    key_file = Path(document["blitzecdn_security_under_attack_key_file"])
    key_file.parent.mkdir()
    key_file.write_bytes(_PLACEHOLDER)

    inventory = tmp_path / "inventory"
    shutil.copytree(CORE_ANSIBLE / "inventory" / "group_vars", inventory / "group_vars")
    (inventory / "hosts.yml").write_text(
        yaml.safe_dump(
            {
                "blitzecdn_edges": {
                    "hosts": {
                        "edge-a": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": (
                                "{{ ansible_playbook_python }}"
                            ),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    variables = tmp_path / "release.json"
    variables.write_text(json.dumps(document), encoding="utf-8")
    playbook = tmp_path / "render.yml"
    _playbook(control, playbook)
    (tmp_path / "ansible-local").mkdir()
    completed = subprocess.run(  # noqa: S603 -- fixed argv over tmp_path
        [
            executable,
            "-i",
            str(inventory / "hosts.yml"),
            str(playbook),
            "--check",
            "--diff",
            "--extra-vars",
            f"@{variables}",
            "--extra-vars",
            json.dumps(control.edge_contributions.variables()),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env=os.environ
        | {
            "ANSIBLE_STDOUT_CALLBACK": "json",
            "ANSIBLE_MAX_DIFF_SIZE": str(16 * 1024 * 1024),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-local"),
            "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
            # The shipped core roles, then each capability's: the test
            # settings put the control plane's own Ansible tree in tmp_path.
            "ANSIBLE_ROLES_PATH": os.pathsep.join(
                [
                    str(CORE_ANSIBLE / "roles"),
                    *(str(path) for path in control.edge_contributions.roles_path[1:]),
                ]
            ),
            "ANSIBLE_COLLECTIONS_PATH": str(PROJECT_DIR / ".state/collections"),
            "BLITZE_UNDER_ATTACK_SECRET": _SECRET,
        },
    )
    assert completed.returncode == 0, _failures(completed)
    return _captured(json.loads(completed.stdout), candidate)


def _failures(completed: subprocess.CompletedProcess[str]) -> str:
    """The failed tasks and their messages, rather than the whole report."""
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed.stdout[-4000:] + completed.stderr
    failed = [
        f"{task['task']['name']}: {item.get('msg')}"
        for play in report["plays"]
        for task in play["tasks"]
        for result in [task["hosts"].get("edge-a", {})]
        for item in [result, *result.get("results", [])]
        if item.get("failed")
    ]
    return "\n".join(failed) or completed.stderr


def _captured(
    report: dict[str, Any], candidate: Path
) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    links: dict[str, str] = {}
    for play in report["plays"]:
        for task in play["tasks"]:
            result = task["hosts"].get("edge-a", {})
            for item in [result, *result.get("results", [])]:
                if item.get("skipped"):
                    continue
                diffs = item.get("diff") or []
                for diff in diffs if isinstance(diffs, list) else [diffs]:
                    if isinstance(diff.get("after"), str):
                        destination = diff.get("before_header")
                        if not destination:
                            # A copy of host material (a certificate) names
                            # its source; a rendered template names a `.j2`.
                            assert not str(diff.get("after_header")).endswith(".j2"), (
                                f"{task['task']['name']} rendered a file the "
                                "controller renderer did not predict"
                            )
                            continue
                        path = Path(destination)
                        if path.is_relative_to(candidate):
                            files[str(path.relative_to(candidate))] = diff["after"]
                    elif (diff.get("after") or {}).get("state") == "link":
                        path = Path(item["dest"])
                        if path.is_relative_to(candidate):
                            links[str(path.relative_to(candidate))] = item["src"]
    return files, links


def _assert_parity(control: ControlPlane, tmp_path: Path) -> ConfigurationBundle:
    document = _document(control, tmp_path)
    bundle = EdgeRenderer(
        ansible_dir=CORE_ANSIBLE, contributions=control.edge_contributions
    ).render(document)
    files, links = _edge_rendering(control, document, bundle, tmp_path)

    assert sorted(files) == sorted(bundle.files)
    for name, content in bundle.files.items():
        assert content.decode("utf-8") == files[name], name
    assert links == dict(bundle.links)
    return bundle


def test_a_plain_and_a_disabled_tls_site_render_as_the_edge_renders(settings, tmp_path):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    seed_site(control, origin_request_host="origin.example.com", ssl_mode="off")
    seed_site(
        control,
        name="static-example-com",
        record="static",
        enabled=False,
        cache_enabled=False,
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/plain/fullchain.pem",
        certificate_key_path="/etc/ssl/plain/privkey.pem",
        firewall={
            "allow_sources": ["203.0.113.9"],
            "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
            "denied_methods": ["DELETE", "TRACE"],
            "denied_paths": ["/admin", "/.git"],
        },
    )

    bundle = _assert_parity(control, tmp_path)

    disabled = [
        site.name for site in control.sites.list_sites() if not site.policy.enabled
    ]
    assert disabled
    for name in disabled:
        assert f"nginx/sites-available/{name}.conf" in bundle.files
        assert f"nginx/sites-enabled/{name}.conf" not in bundle.links


def test_an_uploaded_certificate_renders_as_the_edge_renders(
    settings, tmp_path, certificate_pair
):
    control = certificate_control_plane(settings)
    site = seed_site(control)
    certificate, key = certificate_pair()
    control.certificates.upload_certificate(site.name, certificate, key, "alice")
    control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(ssl_mode="full", always_use_https=True)),
        "alice",
    )

    bundle = _assert_parity(control, tmp_path)

    rendered = bundle.files["nginx/sites-available/example-com.conf"].decode()
    assert "ssl_certificate " in rendered


def test_fleet_settings_and_under_attack_render_as_the_edge_renders(settings, tmp_path):
    configured = with_capability_settings(settings, under_attack_secret=_SECRET)
    repository = Repository(configured.database_path)
    control = ControlPlane(settings=configured, repository=repository)
    for name, value in {
        "blitzecdn_security_under_attack_enabled": True,
        "blitzecdn_nginx_hsts_enabled": True,
        "blitzecdn_nginx_ssl_stapling": True,
        "blitzecdn_nginx_proxy_read_timeout": "45s",
        "blitzecdn_nginx_default_server": False,
    }.items():
        repository.ansible_settings.set_setting(name, value)
    seed_site(control, under_attack_mode=True, http3_enabled=False)
    seed_site(
        control,
        name="example-org",
        domain="example.org",
        record="api",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/plain/fullchain.pem",
        certificate_key_path="/etc/ssl/plain/privkey.pem",
        http3_enabled=True,
    )

    bundle = _assert_parity(control, tmp_path)

    module = bundle.files["njs/under-attack.js"].decode()
    assert _SECRET not in module
    assert all(_SECRET not in content.decode() for content in bundle.files.values())
    assert "nginx/sites-available/blitzecdn-default.conf" not in bundle.files
