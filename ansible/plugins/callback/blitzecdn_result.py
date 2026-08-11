"""Report what a playbook did as structured JSON, for the control plane to read.

This is the contract that replaced parsing ``PLAY RECAP``. Ansible's terminal
output is written for a person: it can be reworded between releases, it flattens
"failed inside a task" and "never started" into similar-looking text, and it can
say that three tasks would change without saying which. A callback is handed the
task objects, so none of that has to be inferred.

The control plane sets ``BLITZE_RESULT_PATH`` to a path belonging to that one
invocation and reads the document back when the process exits. With the variable
unset this plugin does nothing at all, so an operator running ``ansible-playbook``
by hand gets ordinary behaviour and no stray files.

Raw output is not this plugin's business. It goes to a log file the runner opens,
and nothing in the control plane parses it.

Kept deliberately small and dependency-free — it runs inside Ansible's process,
where an exception in a callback is reported as a warning and the run continues
without its result document.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: blitzecdn_result
    type: aggregate
    short_description: Write one structured JSON result per playbook run.
    description:
      - Records per-host task counters, the tasks that changed, the tasks that
        failed, and any payload a role published via the C(blitzecdn_report)
        fact.
      - Writes to the path in the C(BLITZE_RESULT_PATH) environment variable
        and is inert when that variable is unset.
    requirements:
      - Enable with C(callbacks_enabled) in ansible.cfg.
"""

#: A role returns data to the control plane by setting this fact. It is the
#: only supported channel for a payload rather than an outcome — see
#: ``blitzecdn.edge.blitzecdn_stats``, which reports its counters this way.
REPORT_FACT = "blitzecdn_report"

#: Failure text is for a human reading a report, not for a machine to match on.
#: Long module output belongs in the log file; this keeps a result document
#: readable and bounded when a task fails across a large fleet.
_MAX_MESSAGE = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "blitzecdn_result"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._path = os.environ.get("BLITZE_RESULT_PATH") or None
        self._playbook = ""
        self._started_at = _now()
        self._hosts: dict[str, dict] = {}

    # -- Ansible hooks -------------------------------------------------

    def v2_playbook_on_start(self, playbook):
        self._playbook = getattr(playbook, "_file_name", "") or ""

    # These hooks collect *which* tasks did what. They deliberately keep no
    # counts: `v2_playbook_on_stats` takes those from Ansible's own tallies,
    # which already account for rescued blocks and ignored errors.
    def v2_runner_on_ok(self, result):
        host = self._host(result)
        fields = getattr(result, "_result", {}) or {}
        if fields.get("changed"):
            self._record(host, result, "changed")
        report = (fields.get("ansible_facts") or {}).get(REPORT_FACT)
        if isinstance(report, dict):
            # Last writer wins. A role that publishes twice for one host means
            # the later document supersedes the earlier one.
            host["report"] = report

    def v2_runner_on_failed(self, result, ignore_errors=False):
        # An ignored error is a task the play chose to continue past, so it is
        # not something the control plane should treat as a failed host.
        if not ignore_errors:
            self._record(self._host(result), result, "failed")

    def v2_runner_on_unreachable(self, result):
        self._record(self._host(result), result, "unreachable")

    # An item within a loop is reported per item and then once for the task as
    # a whole. The per-item call is the one that carries which item failed.
    def v2_runner_item_on_failed(self, result):
        self._record(self._host(result), result, "failed")

    def v2_playbook_on_stats(self, stats):
        """Reconcile against Ansible's own tallies, then write the document.

        The per-task hooks above miss what a run does not report through them —
        a host rescued by a ``rescue:`` block, most obviously. Ansible's summary
        is authoritative for the counts, so it wins; the task lists this plugin
        gathered are what the summary cannot provide.
        """
        for name in stats.processed:
            host = self._hosts.setdefault(name, _blank(name))
            summary = stats.summarize(name)
            host.update(
                ok=summary.get("ok", 0),
                changed=summary.get("changed", 0),
                failed=summary.get("failures", 0),
                unreachable=summary.get("unreachable", 0),
                skipped=summary.get("skipped", 0),
                rescued=summary.get("rescued", 0),
                ignored=summary.get("ignored", 0),
            )
        self._write()

    # -- Internals -----------------------------------------------------

    def _host(self, result) -> dict:
        name = result._host.get_name()
        return self._hosts.setdefault(name, _blank(name))

    def _record(self, host, result, outcome):
        task = getattr(result, "_task", None)
        entry = {
            "task": (getattr(task, "get_name", lambda: "")() or "").strip()
            or "unnamed task",
            "action": getattr(task, "action", "") or "",
            "outcome": outcome,
            "role": self._role(task),
        }
        if outcome == "changed":
            host["changes"].append(entry)
            return
        entry["message"] = self._message(result)
        host["failures"].append(entry)

    @staticmethod
    def _role(task) -> str | None:
        role = getattr(task, "_role", None)
        name = getattr(role, "_role_name", None) if role else None
        return str(name) if name else None

    @staticmethod
    def _message(result) -> str:
        fields = getattr(result, "_result", {}) or {}
        for key in ("msg", "stderr", "stdout", "reason", "exception"):
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_MESSAGE]
        return "no message"

    def _write(self) -> None:
        if not self._path:
            return
        document = {
            "playbook": self._playbook,
            "started_at": self._started_at,
            "finished_at": _now(),
            "hosts": [self._hosts[name] for name in sorted(self._hosts)],
        }
        # Written through a temporary file in the same directory so the runner
        # never reads a partial document: it looks for this path only after the
        # process has exited, but a rename is the cheap way to be certain.
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            handle, staged = tempfile.mkstemp(dir=directory, suffix=".partial")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True)
            os.replace(staged, self._path)
        except OSError as error:
            # A result the control plane cannot read is reported as a run that
            # produced no structured output, which it treats as a failure. Say
            # why here rather than leaving that unexplained.
            self._display.warning(f"blitzecdn_result could not write a result: {error}")


def _blank(name: str) -> dict:
    return {
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
    }
