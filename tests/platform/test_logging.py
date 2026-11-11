import io
import json
import logging
import sys

from blitzecdn.core.runtime.logging import (
    configure_logging,
    get_logger,
    json_formatter,
)


def _one(record: logging.LogRecord) -> dict[str, object]:
    payload: dict[str, object] = json.loads(json_formatter().format(record))
    return payload


def test_json_formatter_and_configuration():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "safe %s", ("value",), None
    )
    payload = _one(record)
    assert payload["message"] == "safe value"
    assert payload["level"] == "info"
    assert payload["logger"] == "test"
    configure_logging(verbose=True, json_output=True)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("alembic").level == logging.NOTSET


def test_normal_logging_keeps_routine_migration_chatter_out_of_cli_output():
    configure_logging()

    assert logging.getLogger("alembic").level == logging.WARNING


def test_keyword_context_becomes_fields_of_the_line():
    """Our events carry context as keyword arguments, not `extra=` dictionaries."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(json_formatter())
    logger = logging.getLogger("blitzecdn.test.fields")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        get_logger("blitzecdn.test.fields").info(
            "converged", deployment_id="abc123", site="example-com"
        )
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "converged"
    assert payload["logger"] == "blitzecdn.test.fields"
    assert payload["deployment_id"] == "abc123"
    assert payload["site"] == "example-com"
    assert list(payload)[:4] == ["timestamp", "level", "logger", "message"]


def test_a_field_named_like_a_record_attribute_does_not_raise():
    """`extra={"name": ...}` raises KeyError inside stdlib; a keyword does not."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(json_formatter())
    logger = logging.getLogger("blitzecdn.test.collision")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        get_logger("blitzecdn.test.collision").info(
            "collected", name="edge-a", args=("x",), module="m"
        )
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue())
    assert payload["name"] == "edge-a"
    assert payload["module"] == "m"


def test_context_a_third_party_attaches_with_extra_survives_formatting():
    record = logging.LogRecord(
        "uvicorn", logging.INFO, __file__, 1, "converged", None, None
    )
    record.deployment_id = "abc123"

    payload = _one(record)

    assert payload["deployment_id"] == "abc123"
    # And nothing internal leaks in alongside it.
    assert "args" not in payload
    assert "msg" not in payload
    assert "_record" not in payload


def test_the_timestamp_is_when_it_happened_not_when_it_was_formatted():
    """Formatting order is not event order under any queueing."""
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "old", None, None)
    record.created = 1_700_000_000.0

    payload = _one(record)

    assert str(payload["timestamp"]).startswith("2023-11-14")


def test_an_unserialisable_value_does_not_lose_the_line():
    """A log line is diagnostics; it must not raise out of the thing it reports."""
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "odd", None, None)
    record.detail = object()

    payload = _one(record)

    assert "object object at" in str(payload["detail"])


def test_an_exception_is_rendered_into_the_line():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )

    payload = _one(record)

    assert "RuntimeError: boom" in str(payload["exception"])


def test_plain_output_is_level_message_and_context():
    configure_logging()
    (handler,) = logging.getLogger().handlers
    record = logging.LogRecord(
        "test", logging.WARNING, __file__, 1, "careful", None, None
    )
    record.job_id = "j1"

    assert handler.format(record) == "WARNING careful job_id=j1"
