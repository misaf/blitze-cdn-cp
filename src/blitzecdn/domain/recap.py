from __future__ import annotations

import re

from blitzecdn.domain.models import HostDrift

#: A line of Ansible's PLAY RECAP: ``host : ok=5 changed=1 unreachable=0 ...``.
#:
#: The recap is a stable, human-facing summary Ansible has printed in this
#: shape for its whole 2.x life. Parsing it avoids requiring a callback plugin
#: or JSON output, either of which would change what an operator sees in
#: `blitzecdn status`. Unparseable lines are ignored rather than guessed at.
_RECAP_LINE = re.compile(r"^(?P<host>\S+)\s*:\s+(?P<stats>(?:\w+=\d+\s*)+)$")
_RECAP_HEADER = "PLAY RECAP"


def parse_play_recap(stdout: str) -> tuple[HostDrift, ...]:
    """Read per-host task counts out of an Ansible run's output.

    Only the section after the final ``PLAY RECAP`` header is read: a run that
    executed more than one play prints one recap per play, and the last is the
    cumulative one.

    Lives in the domain rather than beside the runner because it is a pure
    string-to-model translation with no process, no filesystem and no Ansible
    import — and because the application layer reads recaps out of *stored*
    deployment output, long after any runner has finished.
    """
    _, marker, tail = stdout.rpartition(_RECAP_HEADER)
    if not marker:
        return ()
    hosts: list[HostDrift] = []
    for line in tail.splitlines():
        match = _RECAP_LINE.match(line.strip())
        if match is None:
            continue
        counts = {
            name: int(number)
            for name, _, number in (
                field.partition("=") for field in match["stats"].split()
            )
        }
        hosts.append(
            HostDrift(
                host=match["host"],
                changed=counts.get("changed", 0),
                ok=counts.get("ok", 0),
                failed=counts.get("failed", 0),
                unreachable=counts.get("unreachable", 0),
            )
        )
    return tuple(hosts)
