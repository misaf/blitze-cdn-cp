from __future__ import annotations

import os
import re
import sys

from blitzecdn.core.ansible import AnsibleRunner
from blitzecdn.core.config import Settings
from blitzecdn.core.database import Repository

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
    settings = Settings.from_environment()
    # The runner reads the fleet to expand a host limit. This hook never sets
    # one — an HTTP-01 challenge has to be published on every edge, because the
    # CA may validate against any of them — but the runner still needs the
    # store, and it must be the same database the inventory plugin will read.
    repository = Repository(settings.database_path)
    try:
        runner = AnsibleRunner(settings, repository.edges)
        run = runner.run_acme_challenge(
            action=action,
            domain=domain,
            token=token,
            validation=validation,
        )
    finally:
        repository.close()
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
