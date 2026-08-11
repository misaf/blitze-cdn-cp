from __future__ import annotations

import os
import re
import sys

from blitzecdn.config import Settings
from blitzecdn.infrastructure.ansible import AnsibleRunner

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_VALIDATION = re.compile(r"^[A-Za-z0-9_.-]{1,2048}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    domain = os.environ.get("CERTBOT_DOMAIN", "")
    token = os.environ.get("CERTBOT_TOKEN", "")
    validation = os.environ.get("CERTBOT_VALIDATION", "")
    if action not in {"present", "absent"}:
        return _fail("invalid ACME hook action")
    if not _DOMAIN.fullmatch(domain) or not _TOKEN.fullmatch(token):
        return _fail("invalid ACME challenge environment")
    if action == "present" and not _VALIDATION.fullmatch(validation):
        return _fail("invalid ACME validation value")
    run = AnsibleRunner(Settings.from_environment()).run_acme_challenge(
        action=action,
        domain=domain,
        token=token,
        validation=validation,
    )
    if not run.succeeded:
        # certbot shows this to the operator when issuance fails, so it has to
        # be the useful line: run.summary() names the task and the edge, and
        # the full output stays in the run log it points at.
        return _fail(f"{run.summary()} (full output: {run.log_path})")
    return 0


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
