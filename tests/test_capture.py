"""The capture path: Loom's logger -> LoomLogHandler -> sink.

This is the app's central claim — "every generate() call in the process is
recorded, with no call-site changes" — and all of it happens through global
logging state, which is exactly the kind of thing that breaks silently.
"""

from __future__ import annotations

import logging

import pytest
from loom._logging import log_call
from loom.observability import LoomLogHandler

import app as appmod
import metrics


@pytest.fixture
def wired(tmp_path):
    """A capture wired to a temp DB, with global logger state restored after."""
    logger = logging.getLogger("loom")
    before = (list(logger.handlers), logger.level, logger.propagate)
    sink = appmod._install_capture(str(tmp_path / "events.db"))
    yield sink
    sink.close()
    logger.handlers, logger.level, logger.propagate = before


def _emit_success(**over):
    payload = {
        "provider": "openai",
        "modality": "text",
        "model": "gpt-4o",
        "upstream_model": "gpt-4o",
        "latency_ms": 123.4,
        "result": {
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "cost": {"usd": 0.002, "local": 0.17, "local_currency": "INR"},
        },
    }
    payload.update(over)
    log_call(**payload)


def test_successful_call_is_captured(wired):
    _emit_success()

    rows = metrics.tail(wired)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-4o"
    assert row["latency_ms"] == 123.4
    assert row["total_tokens"] == 15
    assert row["cost_usd"] == 0.002
    assert row["ok"] == 1


def test_logger_level_lets_info_through(wired):
    # log_call emits successes at INFO, and the `loom` logger inherits root's
    # WARNING by default. Without the explicit setLevel every successful call
    # is dropped before any handler sees it, leaving a dashboard that shows
    # only failures. Guard the level itself, not just the happy path.
    assert logging.getLogger("loom").level == logging.INFO
    assert logging.getLogger("loom").isEnabledFor(logging.INFO)


def test_failed_call_is_captured_with_error_fields(wired):
    log_call(
        provider="xai", modality="text", model="grok-2", upstream_model="grok-2",
        latency_ms=2400.0, result=None, error=TimeoutError("timed out"), retries=2,
    )

    row = metrics.tail(wired)[0]
    assert row["ok"] == 0
    assert row["error_type"] == "TimeoutError"
    assert row["retries"] == 2
    assert metrics.recent_retries(wired)[0]["provider"] == "xai"


def test_tags_survive_the_round_trip(wired):
    # How playground calls stay distinguishable from demo rows.
    _emit_success(tags={"source": "playground"})
    assert metrics.tail(wired)[0]["tags"] == {"source": "playground"}


def test_unrelated_log_records_are_ignored(wired):
    logging.getLogger("loom").info("just a log line, no payload")
    logging.getLogger("loom.retry").warning("retrying")

    assert metrics.tail(wired) == []


def test_capture_is_not_stacked_by_a_second_install(wired, tmp_path):
    # A second create_app()/install in one process must not double-write.
    second = appmod._install_capture(str(tmp_path / "second.db"))
    try:
        logger = logging.getLogger("loom")
        assert sum(isinstance(h, LoomLogHandler) for h in logger.handlers) == 1

        _emit_success()
        assert second.count() == 1
        assert wired.count() == 0
    finally:
        second.close()
