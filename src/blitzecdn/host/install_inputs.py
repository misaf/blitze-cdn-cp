"""Validate the installer's operator inputs before the host is converged.

Run by `install.sh` as ``python -I -m`` in the control-plane image it is about
to converge with, so the package that will later read these values is the one
checking them. ``allowed-ips`` is therefore not a restatement: it applies
:func:`~blitzecdn.core.config.settings.allowed_ip_networks`, the rule ``Settings``
applies to ``BLITZE_ALLOWED_IPS``, and cannot accept a value the API would refuse
at boot. ``standalone`` checks inputs no setting holds, so its rules live here.
Exit 1 with one message on stderr refuses.
"""

from __future__ import annotations

import argparse
import ipaddress
from collections.abc import Sequence

from blitzecdn.core.config.settings import allowed_ip_networks


class RefusedError(Exception):
    """The input is invalid. The argument is the message for the operator."""


def check_allowed_ips(value: str) -> None:
    """The setting's rule, as the flag spells it.

    The installer joins repeated flags with commas and calls this only when one
    was given, so an empty string here is a flag with no value and is refused;
    the environment's reading of a blank string as "loopback only" is not this
    caller's to borrow.
    """
    try:
        allowed_ip_networks(value.split(","), label="--allowed-ips")
    except ValueError as error:
        raise RefusedError(f"error: {error}") from error


def check_standalone(admin_cidr: str, email: str) -> None:
    """A parseable SSH administration network and a plausible ACME email."""
    try:
        ipaddress.ip_network(admin_cidr, strict=False)
    except ValueError as error:
        raise RefusedError(f"error: invalid --admin-cidr: {error}") from error
    if email.count("@") != 1 or any(char.isspace() for char in email):
        raise RefusedError("error: --email must be a valid email address")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install-inputs")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("allowed-ips").add_argument("value")
    standalone = commands.add_parser("standalone")
    standalone.add_argument("admin_cidr")
    standalone.add_argument("email")
    options = parser.parse_args(arguments)
    try:
        if options.command == "allowed-ips":
            check_allowed_ips(options.value)
        else:
            check_standalone(options.admin_cidr, options.email)
    except RefusedError as refused:
        parser.exit(1, f"{refused.args[0]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
