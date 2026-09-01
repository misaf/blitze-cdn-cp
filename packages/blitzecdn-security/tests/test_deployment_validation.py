"""The security check where it actually runs: inside `deployments.validate()`.

The unit tests beside this one hold the hook to its answer. These hold the
control plane to what it does with that answer — refusing *before* Ansible is
invoked is the whole reason the check exists, and only the real service can
show that no scratch file was rendered and no playbook was handed one.

Sites are seeded through DNS rather than written to the site store, because
desired state is derived from proxied records: a site the store knows about and
no record derives is not in the snapshot the validator reads.
"""

import json

import pytest
from control_plane_fixtures import FakeRunner, ansible_run, host_run
from pydantic import SecretStr
from typer.testing import CliRunner

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.cli import main as cli
from blitzecdn.core.database import Repository
from blitzecdn.features.deployments.domain import DeploymentStatus
from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn_security.config import SECRET_VARIABLE

runner = CliRunner()

#: Long enough for this capability to sign a clearance with.
_SECRET = SecretStr("s" * 32)

_REFUSAL = (
    "security: cdn-example-com: under_attack_mode is on but "
    f"{SECRET_VARIABLE} is not set to at least 32 bytes on this controller, so "
    "the edge challenge capability cannot be enabled and the deployment would "
    "fail on every edge."
)


def _control_serving(settings, runner_stub, **patch):
    """A control plane serving one proxied site, patched with `patch`."""
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=runner_stub,
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    if patch:
        control.dns.update_record(
            "example.com", "cdn", RecordType.A, RecordPatch(**patch), "alice"
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
    """A blocking plugin issue is reported like every other validation error.

    Same exit code, same `--json` envelope, and the `plugin: site: message`
    attribution the deployment service already applied to plugin issues. The
    refusal adds a reason to fail, not a new way of failing.
    """
    control = _control_serving(unprovisioned, FakeRunner(), under_attack_mode=True)
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: unprovisioned)

    result = runner.invoke(cli.app, ["validate", "--json"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert json.loads(result.output) == {"valid": False, "errors": [_REFUSAL]}
