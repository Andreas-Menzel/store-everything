"""Structured logging: one JSON object per line, always correlatable."""

from __future__ import annotations

import json
import logging

from store_everything.log import JsonFormatter, request_id_var


def _record(**kwargs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def test_record_is_rendered_as_one_json_object() -> None:
    payload = json.loads(JsonFormatter().format(_record()))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello world"
    assert payload["ts"].endswith("+00:00")


def test_request_id_is_attached_when_one_is_in_scope() -> None:
    token = request_id_var.set("req_0123456789abcdef0123456789abcdef")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)

    assert payload["request_id"] == "req_0123456789abcdef0123456789abcdef"


def test_no_request_id_key_outside_a_request() -> None:
    payload = json.loads(JsonFormatter().format(_record()))

    assert "request_id" not in payload


def test_extra_fields_are_kept_as_structured_data() -> None:
    payload = json.loads(JsonFormatter().format(_record(status=404, path="/healthz")))

    assert payload["status"] == 404
    assert payload["path"] == "/healthz"


def test_terminal_colour_duplicates_are_dropped() -> None:
    """uvicorn ships an ANSI-coloured copy of its messages; structured logs don't want it."""
    payload = json.loads(JsonFormatter().format(_record(color_message="hello \x1b[36m%s\x1b[0m")))

    assert "color_message" not in payload
    assert "\x1b" not in json.dumps(payload)


def test_exceptions_are_rendered_into_the_object() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]
