"""`cert` — inspect, preflight, issue, upload, renew and reconcile TLS material."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn_certificates.certificates.domain import CERTIFICATE_RENEWAL_DAYS
from blitzecdn_certificates.composition import build_certificate_service

cert_app = typer.Typer(
    no_args_is_help=True, help="Inspect, issue and renew managed TLS certificates."
)

#: What ``upload`` will read from a file, matching the two ``read`` bounds the
#: upload route passes to its multipart parts. Stated once here and once there
#: rather than shared, because they are bounding different things — a request
#: body the control plane did not choose to accept, and a local file the
#: operator named — and only the second can say "that is not a PEM, check the
#: path" instead of dropping the connection.
PEM_CERTIFICATE_LIMIT = 1_048_576
PEM_PRIVATE_KEY_LIMIT = 262_144


def _read_material(path: Path, limit: int) -> bytes:
    """The file's bytes, or a CLI error naming the path that was wrong.

    Refuses an oversized file by its size rather than by truncating to the
    limit: a chain cut off mid-PEM would reach the parser as a malformed
    certificate, and the operator would go looking at the certificate instead
    of at the path they typed.
    """
    try:
        material = path.read_bytes()
    except OSError as error:
        raise typer.BadParameter(f"cannot read {path}: {error.strerror}") from error
    if len(material) > limit:
        raise typer.BadParameter(
            f"{path} is {len(material)} bytes; PEM material is expected to be "
            f"under {limit}"
        )
    return material


@cert_app.command("list")
def cert_list(
    expiring_in: Annotated[
        int | None,
        typer.Option(
            "--expiring-in",
            help="Show only certificates with at most this many days left.",
        ),
    ] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """List managed certificates, soonest expiry first.

    Exits 4 if any listed certificate has already expired, so a scheduled
    check notices without anyone reading the output.
    """
    control = common.control_plane()
    statuses = (
        build_certificate_service(control).expiring_certificates(expiring_in)
        if expiring_in is not None
        else build_certificate_service(control).certificate_statuses()
    )
    common.emit(statuses, json_output=json_output)
    if not json_output and not statuses:
        typer.echo("No managed certificates.")
    if any(status.expired for status in statuses):
        raise typer.Exit(ExitCode.CONFLICT)


@cert_app.command("preflight")
def cert_preflight(
    name: Annotated[str, typer.Argument(help="Site name.")],
    json_output: common.JsonOutput = False,
) -> None:
    """Check whether a certificate could be issued for a site right now.

    Looks at what issuance actually depends on and the control plane cannot
    control: that the hostname resolves to one of our edges, that CAA permits
    our CA, that the vhost is already deployed, plus advisories on the origin
    and the record's TTL. Contacts no CA and changes nothing, so it is safe to
    run repeatedly while a customer is still moving their DNS.

    Exits 3 if anything blocks issuance. Advisories alone do not.
    """
    report = build_certificate_service(common.control_plane()).certificate_preflight(
        name
    )
    common.emit(report, json_output=json_output)
    if not json_output:
        for check in report.checks:
            mark = "ok  " if check.passed else f"{check.severity.value.upper()}"
            typer.echo(f"  {mark:9} {check.name}: {check.detail}")
        if report.ok:
            typer.echo(f"\n{name} is ready for issuance.")
        else:
            typer.echo(f"\n{name} cannot be issued yet: {report.summary()}", err=True)
    if not report.ok:
        raise typer.Exit(ExitCode.CONFIGURATION)


@cert_app.command("renew")
def cert_renew(
    expiring_in: Annotated[
        int,
        typer.Option(
            "--expiring-in", help="Renew certificates with at most this many days left."
        ),
    ] = CERTIFICATE_RENEWAL_DAYS,
    force: Annotated[
        bool,
        typer.Option("--force", help="Renew every ACME certificate regardless of age."),
    ] = False,
    site: Annotated[
        list[str] | None,
        typer.Option(
            "--site",
            help=(
                "Renew only this site; repeat the option to add more. "
                "Without it every managed certificate is considered."
            ),
        ),
    ] = None,
    deploy_after: Annotated[
        bool,
        typer.Option(
            "--deploy",
            help="Deploy once after successful renewals so edges receive them.",
        ),
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Reissue ACME certificates that are close to expiry.

    Safe to run on a schedule: a certificate that is not yet due is left
    alone, and one site failing does not stop the others. Renewed
    certificates reach the edges on the next deploy.

    Use --site to retry a single failure without sending every other
    subscription back to the CA, which is rate limited.
    """
    control = common.control_plane()
    result = build_certificate_service(control).renew_certificates(
        "cli",
        within_days=expiring_in,
        force=force,
        sites=site or None,
        budget_seconds=None,
    )
    deployment = None
    if deploy_after and result.renewed and not result.failed:
        deployment = control.deployments.deploy("cli")
    output: dict[str, Any] = result.model_dump(mode="json")
    if deploy_after:
        output["deployment"] = (
            deployment.model_dump(mode="json") if deployment is not None else None
        )
    common.emit(output, json_output=json_output)
    # Only the prose summary is suppressed under --json. The exit code below
    # still has to fire: --json is the scheduled-run path, and that is exactly
    # the caller that has nothing but the exit code to alert on.
    if not json_output:
        if result.renewed and not deploy_after:
            typer.echo(
                f"\nRenewed {len(result.renewed)} certificate(s). Run "
                "'blitzecdn deploy' to install them on the edges."
            )
        for problem in result.skipped:
            typer.echo(f"  - {problem}", err=True)
        for problem in result.failed:
            typer.echo(f"  - renewal failed: {problem}", err=True)
    if result.failed or (
        deployment is not None and deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@cert_app.command("reconcile")
def cert_reconcile(
    json_output: common.JsonOutput = False,
) -> None:
    """Issue ready first certificates and deploy them to the edge fleet.

    Safe to schedule frequently: sites with certificates are ignored, blocked
    preflights never contact the CA, and the deployment runs only after at
    least one new certificate was issued.
    """
    result = build_certificate_service(common.control_plane()).reconcile_certificates(
        "cli"
    )
    common.emit(result, json_output=json_output)
    deployment = result.deployment
    if result.failed or (
        deployment is not None and deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@cert_app.command("upload")
def cert_upload(
    name: Annotated[str, typer.Argument(help="Site name.")],
    certificate_file: Annotated[
        Path,
        typer.Option(
            "--certificate",
            help="PEM chain, leaf first. Read locally and installed on the edges.",
        ),
    ],
    private_key_file: Annotated[
        Path, typer.Option("--private-key", help="PEM private key for that chain.")
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Install a certificate you already hold for one site.

    The counterpart of ``POST /v1/hosts/{name}/certificate/upload``, which
    takes the same two files as a multipart body. Paths rather than a body
    because that is the shape the material is already in on an operator's
    machine, and reading it here keeps a private key out of the argument
    vector, where ``ps`` and the shell history would both find it.

    The material becomes controller-managed: the site moves to
    ``certificate_mode='uploaded'`` and the control plane owns its paths from
    then on. Nothing is renewed automatically — an uploaded certificate has no
    CA account behind it, so replacing it before expiry means running this
    again. Run 'blitzecdn deploy' afterwards to install it on the edges.
    """
    certificate_pem = _read_material(certificate_file, PEM_CERTIFICATE_LIMIT)
    private_key_pem = _read_material(private_key_file, PEM_PRIVATE_KEY_LIMIT)
    info = build_certificate_service(common.control_plane()).upload_certificate(
        name, certificate_pem, private_key_pem, "cli"
    )
    common.emit(info, json_output=json_output)
    if not json_output:
        typer.echo(
            f"\nInstalled an uploaded certificate for {name}. Run "
            "'blitzecdn deploy' to send it to the edges."
        )


@cert_app.command("request")
def cert_request(
    name: Annotated[str, typer.Argument(help="Site name.")],
    email: Annotated[
        str | None,
        typer.Option(
            "--email",
            help=(
                "ACME account email. Falls back to BLITZE_ACME_DEFAULT_EMAIL, "
                "and issuance is refused if neither is set."
            ),
        ),
    ] = None,
    skip_preflight: Annotated[
        bool,
        typer.Option(
            "--skip-preflight",
            help="Contact the CA even if preflight says validation would fail.",
        ),
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Issue a certificate for one site now, rather than waiting for reconcile.

    ``cert reconcile`` is the scheduled path and issues for every ready site;
    this is the single-site one, for a site whose preflight has just started
    passing and whose certificate should not wait for the next run.

    Preflight runs first and a blocked check refuses the request without
    contacting the CA, because a failed validation spends the CA's rate limit
    just as surely as a successful one. ``--skip-preflight`` overrides that for
    the case preflight cannot see, such as a resolver that answers differently
    from where the edge sits.
    """
    info = build_certificate_service(common.control_plane()).request_certificate(
        name, "cli", email, skip_preflight=skip_preflight
    )
    common.emit(info, json_output=json_output)
    if not json_output:
        typer.echo(
            f"\nIssued a certificate for {name}. Run 'blitzecdn deploy' to "
            "send it to the edges."
        )
