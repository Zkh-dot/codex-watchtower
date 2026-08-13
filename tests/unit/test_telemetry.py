from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from codex_watchtower.telemetry import (
    INPUT_SIZE_BUCKETS_CHARACTERS,
    LATENCY_BUCKETS_SECONDS,
    Counters,
    Histogram,
    Telemetry,
)

# --- Counters ---------------------------------------------------------------


def test_counters_inc_and_value() -> None:
    c = Counters()
    c.inc("events_ingested", {"session_id": "s1"})
    c.inc("events_ingested", {"session_id": "s1"})
    c.inc("events_ingested", {"session_id": "s2"})
    assert c.value("events_ingested", {"session_id": "s1"}) == 2
    assert c.value("events_ingested", {"session_id": "s2"}) == 1
    assert c.value("events_ingested", {"session_id": "s3"}) == 0


def test_counters_inc_amount() -> None:
    c = Counters()
    c.inc("parser_lag", {"session_id": "s1"}, amount=5)
    assert c.value("parser_lag", {"session_id": "s1"}) == 5


def test_counters_as_dict_sanitizes_label_values_into_keys() -> None:
    c = Counters()
    c.inc("events_ingested", {"session_id": "s1"})
    c.inc("unknown_event_types", {"session_id": "s1", "type": "future_event"})
    d = c.as_dict()
    rendered = json.dumps(d)
    assert "s1" in rendered
    assert "future_event" in rendered  # type identifiers are bounded, not transcript text


def test_counters_reset() -> None:
    c = Counters()
    c.inc("events_ingested", {"session_id": "s1"})
    c.reset()
    assert c.value("events_ingested", {"session_id": "s1"}) == 0


# --- Histogram --------------------------------------------------------------


def test_histogram_cumulative_buckets() -> None:
    h = Histogram(buckets=(1.0, 5.0, 10.0))
    h.observe(0.5)
    h.observe(3.0)
    h.observe(7.0)
    d = h.as_dict()
    assert d["count"] == 3
    assert d["+Inf"] == 3
    assert d["buckets"]["1.0"] == 1
    assert d["buckets"]["5.0"] == 2
    assert d["buckets"]["10.0"] == 3
    assert d["sum"] == pytest.approx(10.5)


def test_histogram_reset() -> None:
    h = Histogram(buckets=(1.0,))
    h.observe(0.5)
    h.reset()
    assert h.as_dict()["count"] == 0


def test_latency_buckets_are_monotonic_and_bounded() -> None:
    assert LATENCY_BUCKETS_SECONDS == tuple(sorted(LATENCY_BUCKETS_SECONDS))
    assert LATENCY_BUCKETS_SECONDS[0] > 0


def test_input_size_buckets_are_monotonic_and_bounded() -> None:
    assert INPUT_SIZE_BUCKETS_CHARACTERS == tuple(sorted(INPUT_SIZE_BUCKETS_CHARACTERS))
    assert INPUT_SIZE_BUCKETS_CHARACTERS[0] > 0


# --- Telemetry: structured log redaction ------------------------------------


def _capture_logger() -> tuple[StringIO, logging.Logger]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("watchtower.test.telemetry")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return stream, logger


def test_log_redacts_secrets_in_free_form_fields() -> None:
    stream, logger = _capture_logger()
    tel = Telemetry(logger=logger)
    tel.log(
        "model_call",
        session_id="sess-1",
        detail="Authorization: Bearer sk-abcdefghijklmnop123456",
    )
    line = stream.getvalue().strip()
    record = json.loads(line)
    assert record["event"] == "model_call"
    assert record["session_id"] == "sess-1"
    assert "sk-abcdefghijklmnop123456" not in record["detail"]
    assert "[REDACTED:" in record["detail"]


def test_log_redacts_nested_structures() -> None:
    stream, logger = _capture_logger()
    tel = Telemetry(logger=logger)
    tel.log(
        "test",
        payload={"token": "api_key=supersecretvalue123", "items": ["sk-abcdefghijklmnop12345"]},
    )
    record = json.loads(stream.getvalue().strip())
    assert "supersecretvalue123" not in json.dumps(record)
    assert "sk-abcdefghijklmnop12345" not in json.dumps(record)


def test_log_does_not_emit_when_no_logger() -> None:
    tel = Telemetry()
    tel.log("noop", session_id="s1", detail="anything")
    # no exception, no output -- nothing to assert beyond not raising


# --- Telemetry: counters and histograms -------------------------------------


def test_session_observed_increments_counter() -> None:
    tel = Telemetry()
    tel.session_observed("sess-1")
    assert tel.counters.value("sessions_observed", {"session_id": "sess-1"}) == 1


def test_model_call_records_latency_and_outcome() -> None:
    tel = Telemetry()
    tel.model_call("luna", success=True, latency_seconds=1.3)
    tel.model_call("luna", success=False, latency_seconds=0.2)
    assert tel.counters.value("model_calls", {"profile": "luna", "outcome": "success"}) == 1
    assert tel.counters.value("model_calls", {"profile": "luna", "outcome": "failure"}) == 1
    d = tel.model_latency.as_dict()
    assert d["count"] == 2


def test_notification_counters() -> None:
    tel = Telemetry()
    tel.notification_sent("telegram")
    tel.notification_deduplicated("telegram")
    tel.notification_failed("telegram", "rate_limited")
    assert tel.counters.value("notifications_sent", {"channel": "telegram"}) == 1
    assert tel.counters.value("notifications_deduplicated", {"channel": "telegram"}) == 1
    assert (
        tel.counters.value(
            "notifications_failed", {"channel": "telegram", "reason": "rate_limited"}
        )
        == 1
    )


def test_snapshot_has_all_sections() -> None:
    tel = Telemetry()
    tel.session_observed("s1")
    tel.model_call("luna", success=True, latency_seconds=0.5)
    snap = tel.snapshot()
    assert "counters" in snap
    assert "model_latency" in snap
    assert "input_size" in snap


def test_reset_clears_everything() -> None:
    tel = Telemetry()
    tel.session_observed("s1")
    tel.model_call("luna", success=True, latency_seconds=0.5)
    tel.reset()
    assert tel.counters.value("sessions_observed", {"session_id": "s1"}) == 0
    assert tel.model_latency.as_dict()["count"] == 0


# --- cardinal rule: no transcript content in metrics ------------------------


def test_no_raw_event_summary_reaches_metric_labels() -> None:
    """Free-form event summaries must never become metric label values.

    Type identifiers (bounded enums) are legitimate labels; transcript text
    (summaries, command output, file paths) is not, per spec section 9.
    """
    tel = Telemetry()
    summary_text = "ran pytest tests/unit/test_tailer.py with --verbose and saw failures"
    tel.event_ingested("sess-1")
    tel.unknown_event_type("sess-1", "some_future_event_type")
    snap = json.dumps(tel.snapshot())
    assert summary_text not in snap  # summaries are never passed as labels at all


def test_model_input_size_stores_only_aggregates_not_content() -> None:
    tel = Telemetry()
    packet_content = '{"goal":"implement tailer","events":[...]}'
    tel.model_input_size(characters=len(packet_content))
    snap = json.dumps(tel.snapshot())
    assert packet_content not in snap  # the raw packet is never stored
    assert "sum" in snap  # aggregate character totals are legitimate


def test_log_sanitizes_seeded_secret() -> None:
    stream, logger = _capture_logger()
    tel = Telemetry(logger=logger)
    secret = "sk-abcdefghijklmnop123456"
    tel.log("seeded", detail=f"key was {secret}")
    record = stream.getvalue()
    assert secret not in record
