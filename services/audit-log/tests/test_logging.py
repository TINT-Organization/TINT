import io
import json
import logging

import pytest

from audit_log.logging_config import JsonFormatter, LogLevelError, install_logging, parse_level


def test_parse_level_defaults_and_accepts_case_insensitive_values():
    assert parse_level(None) == logging.INFO
    assert parse_level("") == logging.INFO
    assert parse_level("DEBUG") == logging.DEBUG
    assert parse_level("Warn") == logging.WARNING
    assert parse_level("error") == logging.ERROR


def test_parse_level_rejects_unknown_values():
    with pytest.raises(LogLevelError, match="LOG_LEVEL"):
        parse_level("verbose")


def test_install_logging_writes_contract_json_to_the_given_stream():
    stream = io.StringIO()
    install_logging("audit-log", {"LOG_LEVEL": "info"}, stream=stream)
    logging.getLogger("audit-log").info("migraciones aplicadas")

    entry = json.loads(stream.getvalue())
    assert entry["level"] == "info"
    assert entry["service"] == "audit-log"
    assert entry["message"] == "migraciones aplicadas"
    assert entry["timestamp"].endswith("Z")
    assert "correlation_id" not in entry


def test_install_logging_filters_below_the_configured_level():
    stream = io.StringIO()
    install_logging("audit-log", {"LOG_LEVEL": "warn"}, stream=stream)
    logger = logging.getLogger("audit-log")

    logger.debug("hidden")
    logger.info("hidden")
    logger.warning("shown")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["level"] for line in lines] == ["warn"]


def test_json_formatter_maps_warning_to_warn_and_includes_errors():
    record = logging.LogRecord(
        name="audit-log",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="algo falló",
        args=(),
        exc_info=None,
    )
    entry = json.loads(JsonFormatter("audit-log").format(record))
    assert entry["level"] == "warn"
    assert entry["message"] == "algo falló"
