from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

#: Attributes every ``LogRecord`` carries; anything else came from ``extra=``.
_STANDARD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, keeping whatever ``extra=`` context the caller gave.

    The timestamp is the record's creation time, not formatting time, so ordering
    survives queued or slow handlers.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_FIELDS and not key.startswith("_")
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging(*, verbose: bool = False, json_output: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(levelname)s %(message)s")
    )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True
    )
    # Alembic logs routine INFO on every start, ahead of even `--json` output.
    # Keep warnings; -v restores the full stream.
    logging.getLogger("alembic").setLevel(
        logging.NOTSET if verbose else logging.WARNING
    )
