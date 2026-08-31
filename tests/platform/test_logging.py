import json
import logging

from blitzecdn.core.logging import JsonFormatter, configure_logging


def test_json_formatter_and_configuration():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "safe %s", ("value",), None
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "safe value"
    assert payload["level"] == "info"
    configure_logging(verbose=True, json_output=True)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("alembic").level == logging.NOTSET


def test_normal_logging_keeps_routine_migration_chatter_out_of_cli_output():
    configure_logging()

    assert logging.getLogger("alembic").level == logging.WARNING


def test_context_attached_by_a_caller_survives_formatting():
    """`extra=` is the only way structured context reaches a log line.

    Dropped, the JSON was four fixed fields and prose — nothing to filter on
    when the question is "what happened to this deployment".
    """
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "converged", None, None
    )
    record.deployment_id = "abc123"
    record.site = "cdn-example-com"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["deployment_id"] == "abc123"
    assert payload["site"] == "cdn-example-com"
    # And nothing internal leaks in alongside it.
    assert "args" not in payload
    assert "msg" not in payload


def test_the_timestamp_is_when_it_happened_not_when_it_was_formatted():
    """Formatting order is not event order under any queueing."""
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "old", None, None)
    record.created = 1_700_000_000.0

    payload = json.loads(JsonFormatter().format(record))

    assert payload["timestamp"].startswith("2023-11-14")


def test_an_unserialisable_value_does_not_lose_the_line():
    """A log line is diagnostics; it must not raise out of the thing it reports."""
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "odd", None, None)
    record.detail = object()

    payload = json.loads(JsonFormatter().format(record))

    assert "object object at" in payload["detail"]
