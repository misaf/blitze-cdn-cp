"""Validate the installer's operator inputs before the host is converged.

Run by `install.sh` in the control-plane image it is about to converge with,
as ``python -I``, so standard library only. ``allowed-ips`` restates the rules
``Settings.validate_allowed_ips`` enforces; a test holds the two together.
Exit 1 with one message on stderr refuses.
"""

from __future__ import annotations

import argparse
import ipaddress
from collections.abc import Sequence


class RefusedError(Exception):
    """The input is invalid. The argument is the message for the operator."""


def check_allowed_ips(value: str) -> None:
    """IPv4 networks only, host bits clear, no empty entry."""
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            raise RefusedError(
                "error: --allowed-ips entries must be non-empty IPs/CIDRs"
            )
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise RefusedError(
                f"error: invalid --allowed-ips entry {entry!r}: {error}"
            ) from error
        if network.version != 4:
            raise RefusedError(
                f"error: --allowed-ips entry {entry!r} is IPv6; the API "
                "listens on IPv4 and cannot admit it"
            )
        try:
            ipaddress.ip_network(entry, strict=True)
        except ValueError:
            raise RefusedError(
                f"error: --allowed-ips entry {entry!r} has host bits set. Write "
                f"'{network}' to admit that whole range, or "
                f"'{entry.split('/', 1)[0]}/32' to admit only that address"
            ) from None


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
