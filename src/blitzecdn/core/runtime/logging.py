"""Process logging: structlog for our events, rendered through stdlib handlers.

Our modules log with :func:`get_logger` and pass context as keyword arguments,
``log.info("job handler started", job_id=job.id)``, rather than building
``extra=`` dictionaries, where a key such as ``name`` or ``message`` collides
with a ``LogRecord`` attribute and raises inside the logging call.

Every event still travels as a stdlib ``LogRecord``, so third-party loggers
(uvicorn, alembic, ansible-runner), pytest's ``caplog`` and any handler see one
stream. :func:`configure_logging` decides how that stream renders: one JSON
object per line, or ``LEVEL message`` for a person.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

__all__ = ["configure_logging", "get_logger", "json_formatter"]


def _record_timestamp(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    """When it happened, not when it was formatted: order survives slow handlers."""
    record: logging.LogRecord = event["_record"]
    event["timestamp"] = datetime.fromtimestamp(record.created, UTC).isoformat()
    return event


#: What every event carries, whether it came from structlog or plain stdlib.
_COMMON: tuple[Processor, ...] = (
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
)

#: Applied to stdlib records only: keep the `extra=` context third parties attach.
_FOREIGN: tuple[Processor, ...] = (*_COMMON, structlog.stdlib.ExtraAdder())


#: Already in the plain line, or noise to a person reading it.
_PLAIN_OMITS = frozenset({"event", "level", "logger", "timestamp", "exception"})


def _plain(_: WrappedLogger, __: str, event: EventDict) -> str:
    """``LEVEL message key=value ...`` and any traceback."""
    context = " ".join(
        f"{key}={value}" for key, value in event.items() if key not in _PLAIN_OMITS
    )
    line = f"{event['level'].upper()} {event['event']}"
    if context:
        line += " " + context
    if exception := event.get("exception"):
        line += "\n" + exception
    return line


def _formatter(renderer: Processor) -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=list(_FOREIGN),
        processors=[
            _record_timestamp,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )


def json_formatter() -> logging.Formatter:
    """One JSON object per line: timestamp, level, logger, message, context."""
    return _formatter(_JsonRenderer())


class _JsonRenderer:
    """``event`` is published as ``message``, ahead of the context keys."""

    def __init__(self) -> None:
        self._render = structlog.processors.JSONRenderer(default=str, ensure_ascii=True)

    def __call__(
        self, logger: WrappedLogger, name: str, event: MutableMapping[str, Any]
    ) -> str:
        ordered = {
            "timestamp": event.pop("timestamp"),
            "level": event.pop("level"),
            "logger": event.pop("logger", None),
            "message": event.pop("event"),
            **event,
        }
        return str(self._render(logger, name, ordered))


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        *_COMMON,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    # Not cached, so `structlog.testing.capture_logs` sees every logger.
    cache_logger_on_first_use=False,
)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A logger whose keyword arguments become fields of the event."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def configure_logging(*, verbose: bool = False, json_output: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(json_formatter() if json_output else _formatter(_plain))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True
    )
    # Alembic logs routine INFO on every start, ahead of even `--json` output.
    # Keep warnings; -v restores the full stream.
    logging.getLogger("alembic").setLevel(
        logging.NOTSET if verbose else logging.WARNING
    )
