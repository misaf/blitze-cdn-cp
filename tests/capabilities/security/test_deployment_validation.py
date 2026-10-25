"""The security check where it actually runs: inside `deployments.validate()`.

These show the control plane refuses before Ansible is invoked: no scratch
file rendered, no playbook handed one. Sites are seeded with a routed record,
since a site no hostname reaches contributes no server block.
"""

import json

import pytest
from pydantic import SecretStr
from support.control_plane import (
    FakeRunner,
    ansible_run,
    build_application,
    host_run,
    seed_site,
)
from typer.testing import CliRunner

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.dns.domain import DomainPatch, PatchPolicy
from blitzecdn.capabilities.security.settings import SECRET_VARIABLE
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli

runner = CliRunner()

#: Long enough for this capability to sign a clearance with.
_SECRET = SecretStr("s" * 32)

_REFUSAL = (
    "security: example-com: under_attack_mode is on but "
    f"{SECRET_VARIABLE} is not set on this controller, so the edge challenge "
    "capability cannot be enabled and the deployment would fail on every edge."
)


def _control_serving(settings, runner_stub, **patch):
    """A control plane serving one site, patched with `patch`."""
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=runner_stub,
    )
    seed_site(control, name="example-com", record="cdn")
    if patch:
        control.dns.update_domain(
            "example.com", PatchPolicy.from_patch(DomainPatch(**patch)), "alice"
        )
    return control


@pytest.fixture
def unprovisioned(settings):
    """A controller with no challenge secret — the configuration under test."""
    return settings.model_copy(update={"capability_environment": {}})


@pytest.fixture
def provisioned(settings):
    return settings.model_copy(
        update={"capability_environment": {SECRET_VARIABLE: _SECRET}}
    )


def test_invalid_security_configuration_is_rejected_before_ansible_runs(unprovisioned):
    """No scratch file, no `--syntax-check`, no play. The point of the check."""
    stub = FakeRunner()
    control = _control_serving(unprovisioned, stub, under_attack_mode=True)

    errors = control.deployments.validate()

    assert errors == [_REFUSAL]
    assert stub.validated == []
    assert not unprovisioned.generated_vars_path.exists()


def test_valid_security_configuration_still_validates_and_deploys(provisioned):
    """The same site, with the secret present, converges exactly as before."""
    stub = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = _control_serving(provisioned, stub, under_attack_mode=True)

    assert control.deployments.validate() == []
    assert stub.validated != []

    result = control.deployments.deploy("alice")

    assert result.status is DeploymentStatus.SUCCEEDED
    assert provisioned.generated_vars_path.exists()


@pytest.mark.parametrize(
    ("patch", "why"),
    [
        ({}, "the site never asked for the capability"),
        (
            {"under_attack_mode": True, "enabled": False},
            "a disabled site converges no server block to protect",
        ),
    ],
)
def test_an_unused_security_capability_never_blocks_a_deployment(
    unprovisioned, patch, why
):
    stub = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = _control_serving(unprovisioned, stub, **patch)

    assert control.deployments.validate() == [], why
    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED


def test_cli_error_semantics_are_unchanged_by_the_new_refusal(
    unprovisioned, monkeypatch
):
    """A blocking feature issue is reported like every other validation error.

    Same exit code, same `--json` envelope, same `feature: site: message`
    attribution.
    """
    control = _control_serving(unprovisioned, FakeRunner(), under_attack_mode=True)
    monkeypatch.setattr(cli.common, "services", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: unprovisioned)

    result = runner.invoke(cli.app, ["validate", "--json"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert json.loads(result.output) == {"valid": False, "errors": [_REFUSAL]}
