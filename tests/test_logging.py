import json
import logging

from blitzecdn.logging import JsonFormatter, configure_logging


def test_json_formatter_and_configuration():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "safe %s", ("value",), None
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "safe value"
    assert payload["level"] == "info"
    configure_logging(verbose=True, json_output=True)
    assert logging.getLogger().level == logging.DEBUG
