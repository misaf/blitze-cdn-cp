"""Collect edge-local aggregates through the existing fleet runner."""

from blitzecdn.capabilities.traffic.ansible import COLLECT_PLAYBOOK
from blitzecdn.capabilities.traffic.domain import LogCursor
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.ports.operations import PlaybookRunner


class TrafficPlaybooks:
    def __init__(self, fleet: PlaybookRunner) -> None:
        self._fleet = fleet

    def collect(
        self,
        checkpoints: dict[str, tuple[LogCursor, ...]],
        *,
        host_limit: str | None = None,
    ) -> AnsibleRun:
        return self._fleet.run_playbook(
            name="traffic-collect",
            playbook=COLLECT_PLAYBOOK,
            host_limit=host_limit,
            variables={
                "blitzecdn_traffic_collect_checkpoints": {
                    edge: [cursor.model_dump(mode="json") for cursor in cursors]
                    for edge, cursors in checkpoints.items()
                }
            },
        )
