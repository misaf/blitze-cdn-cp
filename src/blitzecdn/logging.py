from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

#: Attributes every ``LogRecord`` carries. Anything outside this set was put
#: there by a caller passing ``extra=``, which is the only way structured
#: context reaches a log line — so it is exactly what must survive formatting.
_STANDARD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, carrying whatever context the caller attached.

    The fixed four fields are not enough on their own: during an incident the
    questions are "what happened to *this* deployment" and "what did we do to
    *that* site", and a formatter that drops ``extra=`` leaves nothing to
    filter on but prose. Anything a caller attaches is merged in beside the
    message rather than being discarded.

    The timestamp is the record's own creation time, not the moment it reached
    the formatter. Those differ under any queueing or slow handler, and a log
    whose ordering is formatting order is worse than no timestamp at all.
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
