import hashlib
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from support.contracts import (
    PROJECT_DIR,
    ROLE_DIR,
    _role_defaults,
    _seed_site,
    _split_runtime,
)
from support.control_plane import build_application
from support.paths import CORE_ANSIBLE

from blitzecdn.capabilities.dns.domain import Rule
from blitzecdn.composition import Repository
from blitzecdn.core.ansible.rendering import EdgeRenderer

# ----------------------------------------------------------------------
# Executing the role's validation tasks
#
# Reading the role as data or rendering templates evaluates no `when:` or
# `assert`, and neither do `--syntax-check` or ansible-lint: a conditional that
# raises at run time passes all of them.
#
# roles/blitzecdn_nginx/tasks/validate.yml inspects desired state, refuses to
# proceed, and changes nothing on the host, so it runs here against real model
# output.
# ----------------------------------------------------------------------

VALIDATE_TASKS = ROLE_DIR / "tasks/validate.yml"
NGINX_BUILD_INVARIANT_TASKS = ROLE_DIR / "tasks/build-invariant.yml"
NGINX_PROBE_INVARIANT_TASKS = ROLE_DIR / "tasks/probe-invariant.yml"
CONVERGE_TASKS = ROLE_DIR / "tasks/main.yml"


def _run_validation(
    sites: list[dict[str, Any]],
    tmp_path: Path,
    *,
    tasks_file: Path = VALIDATE_TASKS,
    **overrides: Any,
):
    """Execute the role's validation tasks against localhost."""
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    # A contract input is applied before the contract is composed; anything
    # else is an ordinary variable override on top of it.
    inputs, plain = _split_runtime(overrides)
    variables = (
        _role_defaults(**inputs)
        | {
            "blitzecdn_nginx_sites": sites,
        }
        | plain
    )
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir(exist_ok=True)
    playbook = tmp_path / "validate.yml"
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
    return subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        # pytest-cov's subprocess hook would make the Ansible child write
        # statement-only coverage that cannot combine with this run's branch data.
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        # Its own local temp: the ambient one is a single shared directory, and
        # these runs happen concurrently under xdist.
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
        },
        check=False,
    )


def _run_nginx_build_capability(
    tmp_path: Path, configure_arguments: str, *, version: str = "1.27.0"
):
    """Execute the build invariant against a fabricated `nginx -V` banner.

    The invariant reads two variables, so a test can hand it a build that does
    not exist instead of needing an engine and an image.
    """
    return _run_nginx_invariant(
        tmp_path,
        NGINX_BUILD_INVARIANT_TASKS,
        {
            "blitzecdn_nginx_config_test_image": "example/edge:test",
            "blitzecdn_nginx_build_status": 0,
            "blitzecdn_nginx_build_output": (
                f"nginx version: nginx/{version}\n"
                f"configure arguments: {configure_arguments}\n"
            ),
        },
    )


def _run_nginx_probe_invariant(tmp_path: Path, probe: dict[str, Any]):
    """Execute the probe invariant against a fabricated module result.

    A probe that never ran leaves no `status`, which `default(1)` would read as an
    image without nginx; no engine produces that on demand.
    """
    return _run_nginx_invariant(
        tmp_path,
        NGINX_PROBE_INVARIANT_TASKS,
        {
            "blitzecdn_nginx_config_test_image": "example/edge:test",
            "blitzecdn_nginx_build_probe": probe,
        },
    )


def _run_nginx_invariant(tmp_path: Path, tasks_file: Path, variables: dict[str, Any]):
    """Run one of the role's assertion-only task files against localhost."""
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir()
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
    return subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
        [
            shutil.which("ansible-playbook")
            or str(PROJECT_DIR / ".venv/bin/ansible-playbook"),
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
        },
        check=False,
    )


def test_nginx_invariant_accepts_a_capable_build(tmp_path):
    result = _run_nginx_build_capability(
        tmp_path, "--with-http_v3_module --with-http_ssl_module"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nginx_invariant_rejects_an_unsupported_build_clearly(tmp_path):
    result = _run_nginx_build_capability(
        tmp_path, "--with-http_ssl_module", version="not-a-version"
    )
    assert result.returncode != 0
    assert "does not contain a working nginx binary" in result.stdout


def test_nginx_probe_invariant_accepts_a_probe_that_ran(tmp_path):
    """Including one that ran and came back non-zero.

    Judging that is build-invariant.yml's job; this gate only asks whether there
    is a result.
    """
    result = _run_nginx_probe_invariant(tmp_path, {"status": 1, "container": {}})
    assert result.returncode == 0, result.stdout + result.stderr


def test_nginx_probe_invariant_separates_a_probe_that_never_ran(tmp_path):
    """The message must be about the probe, not about the image.

    With `failed_when: false`, a module error arrives as a result with no
    `status`, which must not be reported as an image lacking nginx.
    """
    result = _run_nginx_probe_invariant(
        tmp_path,
        {"failed": True, "msg": "Error starting container: OCI runtime create failed"},
    )
    assert result.returncode != 0
    assert "OCI runtime create failed" in result.stdout
    assert "it is the probe that failed" in result.stdout
    assert "does not contain a working nginx binary" not in result.stdout


def test_role_validation_tasks_actually_run(desired_state, tmp_path):
    """The regression guard for a conditional that only fails at run time."""
    result = _run_validation(desired_state["blitzecdn_nginx_sites"], tmp_path)
    assert result.returncode == 0, (
        "the role's validation tasks failed against real desired state:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_role_accepts_only_immutable_managed_certificate_destinations(
    desired_state, tmp_path
):
    fingerprint = "ab" * 32
    managed = dict(desired_state["blitzecdn_nginx_sites"][0]) | {
        "certificate_mode": "uploaded",
        "certificate_path": (
            f"/etc/blitzecdn/tls/example-com/fullchain-{fingerprint}.pem"
        ),
        "certificate_key_path": (
            f"/etc/blitzecdn/tls/example-com/privkey-{fingerprint}.pem"
        ),
        "certificate_source_path": "/controller/fullchain.pem",
        "certificate_key_source_path": "/controller/privkey.pem",
    }

    accepted = _run_validation([managed], tmp_path)
    assert accepted.returncode == 0, accepted.stdout

    managed["certificate_path"] = "/etc/blitzecdn/tls/example-com/fullchain.pem"
    rejected = _run_validation([managed], tmp_path)
    assert rejected.returncode != 0
    assert "Invalid CDN site" in rejected.stdout


def test_role_accepts_the_connecting_ip_header_without_geoip(desired_state, tmp_path):
    """It reads the connection, not a database, so no capability gates it."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {
        "visitor_headers": {"connecting_ip": True, "ip_country": False}
    }

    result = _run_validation(sites, tmp_path)

    assert result.returncode == 0, result.stdout


def test_role_rejects_a_wildcard_on_an_ip_address(desired_state, tmp_path):
    """Both halves of this control have to refuse it, not just the controller.

    `*.192.0.2.1` passes the hostname shape check and nginx accepts it as a
    `server_name` that matches nothing. The role writes it as root, so it refuses
    it too.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"server_names": ["*.192.0.2.1"]}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "Invalid CDN site" in result.stdout


@pytest.mark.parametrize("field", ["origin_host", "origin_request_host", "origin_sni"])
def test_role_rejects_an_ipv6_zone_index_on_the_origin(desired_state, tmp_path, field):
    """The role's half of the zone-index refusal in `hostname`.

    The role refuses `%` in an origin, keeping values like `fe80::1%x;return 200 x`
    out of `proxy_pass` on its own.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {field: "fe80::1%x;return 200 x"}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "Invalid CDN site" in result.stdout


# Capability refusals (GeoIP without its capability, Under Attack Mode without
# the challenge) are asserted by the capability that makes them, under
# `tests/capabilities/security/`.


# ----------------------------------------------------------------------
# Every installed capability's HTTP fragment is in a release's tree, and a
# detached one's is not. The tree is rendered on the controller and staged as a
# fresh candidate, so nothing is pruned on the edge.
# ----------------------------------------------------------------------


def _http_fragments(renderer: EdgeRenderer, settings) -> set[str]:
    fixture = yaml.safe_load(
        (PROJECT_DIR / "tests/fixtures/desired-state.yml").read_text(encoding="utf-8")
    )
    bundle = renderer.render({**fixture, "blitzecdn_release_digest": "a" * 64})
    prefix = "nginx/conf.d/blitzecdn-feature-"
    return {
        name.removeprefix(prefix) for name in bundle.files if name.startswith(prefix)
    }


def test_a_release_carries_every_installed_capability_fragment(settings):
    control = build_application(settings=settings)
    try:
        installed = {
            resource.name.removesuffix(".j2")
            for resource in control.edge_contributions.nginx_resources["http"]
        }
        assert installed
        assert _http_fragments(control.releases.renderer, settings) == installed
    finally:
        control.close()


def test_a_detached_capabilitys_fragment_is_not_in_the_release(settings):
    control = build_application(settings=settings)
    try:
        contributions = control.edge_contributions
        resources = {
            context: tuple(item for item in items if item.feature != "security")
            for context, items in contributions.nginx_resources.items()
        }
        detached = EdgeRenderer(
            ansible_dir=CORE_ANSIBLE,
            contributions=replace(contributions, nginx_resources=resources),
        )
        fragments = _http_fragments(detached, settings)
        assert "security-http.conf" not in fragments
        assert fragments
    finally:
        control.close()


def test_a_policy_no_hostname_uses_never_reaches_the_edge(settings, tmp_path):
    """A rule nothing matches is not a server block, and neither is a bare zone.

    A host exists because hostnames resolve to it, so one with none cannot be
    published; this checks the derivation leaves them out.
    """
    repository = Repository(settings.database_path)
    control = build_application(settings=settings, repository=repository)
    _seed_site(repository, name="example-com", label="cdn", origin="198.51.100.20")
    # A rule matching a hostname no record claims: the "awaiting DNS" case.
    repository.rules.create_rule(
        Rule(
            domain="example.com",
            name="awaiting-dns",
            match="not-yet.example.com",
            overrides={"cache_enabled": False},
        )
    )
    control.deployments.publish_artifact(
        control.releases.compile(), settings.generated_vars_path
    )
    published = yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))

    names = [site["name"] for site in published["blitzecdn_nginx_sites"]]
    assert names == ["example-com"], (
        "a policy no hostname uses was published to the edge; the role will "
        "refuse an empty server_name and the deploy will fail"
    )
    result = _run_validation(published["blitzecdn_nginx_sites"], tmp_path)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("damage", [False, True])
def test_deployment_checks_the_captured_material_bytes(tmp_path, damage):
    chain = tmp_path / "chain.pem"
    key = tmp_path / "key.pem"
    chain.write_bytes(b"captured chain")
    key.write_bytes(b"captured key")
    material = {
        "certificate_source_path": str(chain),
        "certificate_key_source_path": str(key),
        "certificate_source_sha256": hashlib.sha256(chain.read_bytes()).hexdigest(),
        "certificate_key_source_sha256": hashlib.sha256(key.read_bytes()).hexdigest(),
    }
    if damage:
        key.write_bytes(b"substituted key")
    result = _run_validation(
        [],
        tmp_path,
        tasks_file=ROLE_DIR / "tasks/verify-material.yml",
        blitzecdn_nginx_material_site=material,
    )
    assert (result.returncode == 0) is (not damage), result.stdout + result.stderr
