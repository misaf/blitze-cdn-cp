"""Ansible Runner's event stream, translated into the run contract.

This module is the whole of the translation, and the reason the rest of the
control plane can be held to "never reason from Ansible's textual output": a
structured task or recap event goes in, an
:class:`~blitzecdn.core.domain.runs.AnsibleRun` component comes out, and nothing above
this ever sees the terminal output that also happened.
"""

from __future__ import annotations

from typing import Any

from blitzecdn.core.domain.runs import HostRun, TaskOutcome, TaskResult

__all__ = ["RunnerEvents"]

#: How much of a failing task's message is kept. Ansible will happily hand back
#: a module's entire stdout, and the run record is read by an operator, not
#: replayed.
_MAX_FAILURE_MESSAGE = 2000


class RunnerEvents:
    """Translate Runner's event stream into the control-plane result contract."""

    def __init__(self) -> None:
        self._hosts: dict[str, dict[str, Any]] = {}

    def __call__(self, event: dict[str, Any]) -> bool:
        name = str(event.get("event") or "")
        data = event.get("event_data") or {}
        if not isinstance(data, dict):
            return True
        if name == "playbook_on_stats":
            self._apply_stats(data)
            return True
        host_name = data.get("host")
        if not isinstance(host_name, str) or not host_name:
            return True
        host = self._host(host_name)
        result = data.get("res") or {}
        if not isinstance(result, dict):
            result = {}
        if name == "runner_on_ok":
            if result.get("changed"):
                host["changes"].append(self._task(data, TaskOutcome.CHANGED))
            report = (result.get("ansible_facts") or {}).get("blitzecdn_report")
            if isinstance(report, dict):
                host["report"] = report
        elif name == "runner_on_failed" and not result.get("_ansible_ignore_errors"):
            host["failures"].append(self._task(data, TaskOutcome.FAILED, result))
        elif name == "runner_on_unreachable":
            host["failures"].append(self._task(data, TaskOutcome.UNREACHABLE, result))
        return True

    def hosts(self) -> tuple[HostRun, ...]:
        return tuple(
            HostRun.model_validate(self._hosts[name]) for name in sorted(self._hosts)
        )

    def _host(self, name: str) -> dict[str, Any]:
        return self._hosts.setdefault(
            name,
            {
                "host": name,
                "ok": 0,
                "changed": 0,
                "failed": 0,
                "unreachable": 0,
                "skipped": 0,
                "rescued": 0,
                "ignored": 0,
                "changes": [],
                "failures": [],
                "report": None,
            },
        )

    def _apply_stats(self, data: dict[str, Any]) -> None:
        for event_key, model_key in (
            ("ok", "ok"),
            ("changed", "changed"),
            ("failures", "failed"),
            ("dark", "unreachable"),
            ("skipped", "skipped"),
            ("rescued", "rescued"),
            ("ignored", "ignored"),
        ):
            counts = data.get(event_key) or {}
            if not isinstance(counts, dict):
                continue
            for name, count in counts.items():
                self._host(str(name))[model_key] = int(count)

    @staticmethod
    def _task(
        data: dict[str, Any],
        outcome: TaskOutcome,
        result: dict[str, Any] | None = None,
    ) -> TaskResult:
        message = None
        if result is not None:
            for key in ("msg", "stderr", "stdout", "reason", "exception"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    message = value.strip()[:_MAX_FAILURE_MESSAGE]
                    break
            message = message or "no message"
        return TaskResult(
            task=str(data.get("task") or "unnamed task").strip(),
            action=str(data.get("task_action") or ""),
            outcome=outcome,
            message=message,
            role=str(data["role"]) if data.get("role") else None,
        )
