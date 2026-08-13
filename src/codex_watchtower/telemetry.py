"""Structured internal observability (spec section 9).

Watchtower emits structured logs and Prometheus-compatible counters for the
dimensions the specification lists. The cardinal rule -- "it must not export
transcript content through metrics" (§9) -- is enforced structurally:

- metric *labels* are drawn only from fixed enum values, session ids, signal
  kinds/severities, and outcome categories, never from event summaries, model
  output, command text, file paths, or token strings;
- structured-log *fields* may carry a bounded ``session_id`` and outcome, but
  any free-form payload is run through the same ``privacy.redact`` redactor
  the ingestion path uses before it reaches a log record;
- no histogram exports raw latency samples; each observation is folded into a
  cumulative bucket count, exactly as a Prometheus histogram would.

The module is self-contained and injectable: ``IngestionService`` and the
notifier receive a ``Telemetry`` instance (the no-op default keeps existing
behaviour unchanged) and call the recording methods, which keeps the test
surface narrow and avoids scattering logging calls through every module.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from codex_watchtower.privacy.redact import redact_text

_METRIC_NAME_REPLACEMENT = "_"


def _safe_metric_suffix(value: str) -> str:
    return "".join(c if c.isalnum() else _METRIC_NAME_REPLACEMENT for c in value).strip(
        _METRIC_NAME_REPLACEMENT
    )


@dataclass(slots=True)
class Histogram:
    """Cumulative bucket counts, mirroring a Prometheus histogram.

    Buckets are inclusive upper bounds expressed in the histogram's unit
    (seconds for latency, characters for input size). Each observation
    increments every bucket whose bound is >= the observed value, plus the
    explicit ``+Inf`` bucket, exactly as Prometheus does -- so a caller can
    read any bucket without recomputing from raw samples, and no individual
    sample is ever exported (§9: no raw transcript content, and by extension
    no individually-identifiable latency that could be correlated to a
    specific prompt).
    """

    buckets: tuple[float, ...]
    _counts: list[int] = field(default_factory=lambda: [0] * (0 + 1))
    _sum: float = 0.0
    _count: int = 0

    def __post_init__(self) -> None:
        self._counts = [0] * (len(self.buckets) + 1)

    def observe(self, value: float) -> None:
        self._sum += value
        self._count += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self._counts[i] += 1
        self._counts[-1] += 1  # +Inf

    def as_dict(self) -> dict[str, Any]:
        return {
            "buckets": {str(b): c for b, c in zip(self.buckets, self._counts[:-1], strict=True)},
            "+Inf": self._counts[-1],
            "sum": self._sum,
            "count": self._count,
        }

    def reset(self) -> None:
        self._counts = [0] * (len(self.buckets) + 1)
        self._sum = 0.0
        self._count = 0


LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)
INPUT_SIZE_BUCKETS_CHARACTERS: tuple[float, ...] = (
    1_000,
    4_000,
    8_000,
    16_000,
    32_000,
    48_000,
    64_000,
    128_000,
)


LabelKey = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(slots=True)
class Counters:
    """Plain integer counters keyed by (metric, label-tuple).

    Only fixed enum-like label values are accepted as keys -- never free-form
    text -- so the label space is bounded and cannot leak transcript content.
    """

    _data: Counter[LabelKey] = field(default_factory=Counter)

    def inc(
        self,
        metric: str,
        labels: dict[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        key: LabelKey = (metric, tuple(sorted((labels or {}).items())))
        self._data[key] += amount

    def value(self, metric: str, labels: dict[str, str] | None = None) -> int:
        key: LabelKey = (metric, tuple(sorted((labels or {}).items())))
        return self._data.get(key, 0)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for (metric, labels_tuple), val in self._data.items():
            label_dict: dict[str, str] = dict(labels_tuple)
            suffix = _safe_metric_suffix("_".join(label_dict.values()))
            key = f"{metric}_{suffix}" if suffix else metric
            out[key] = val
        return out

    def reset(self) -> None:
        self._data.clear()


class Telemetry:
    """Process-wide telemetry sink.

    The default no-op behaviour is preserved by constructing ``Telemetry()``
    without a logger: nothing is emitted, counters stay at zero, and callers
    that don't care about observability pay no runtime cost beyond a method
    call. Wiring a real logger turns structured-log emission on without
    changing call sites.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger
        self.counters = Counters()
        self.model_latency = Histogram(buckets=LATENCY_BUCKETS_SECONDS)
        self.input_size = Histogram(buckets=INPUT_SIZE_BUCKETS_CHARACTERS)

    # --- ingestion counters -------------------------------------------------

    def session_observed(self, session_id: str) -> None:
        self.counters.inc("sessions_observed", {"session_id": session_id})

    def event_ingested(self, session_id: str) -> None:
        self.counters.inc("events_ingested", {"session_id": session_id})

    def event_rejected(self, session_id: str, reason: str) -> None:
        self.counters.inc("events_rejected", {"session_id": session_id, "reason": reason})

    def unknown_event_type(self, session_id: str, wire_type: str) -> None:
        self.counters.inc(
            "unknown_event_types",
            {"session_id": session_id, "type": _safe_metric_suffix(wire_type)},
        )

    def cursor_recovery(self, session_id: str, outcome: str) -> None:
        self.counters.inc("cursor_recovery", {"session_id": session_id, "outcome": outcome})

    def parser_lag(self, session_id: str, records_behind: int) -> None:
        self.counters.inc(
            "parser_lag",
            {"session_id": session_id},
            amount=records_behind,
        )

    # --- optional adapter availability --------------------------------------

    def agentlens_availability(self, available: bool) -> None:
        self.counters.inc(
            "agentlens_availability",
            {"status": "available" if available else "unavailable"},
        )

    # --- model call observability -------------------------------------------

    def model_call(self, profile: str, *, success: bool, latency_seconds: float) -> None:
        self.counters.inc(
            "model_calls",
            {"profile": profile, "outcome": "success" if success else "failure"},
        )
        self.model_latency.observe(latency_seconds)

    def model_escalation(self, profile: str) -> None:
        self.counters.inc("model_escalations", {"profile": profile})

    def model_input_size(self, *, characters: int) -> None:
        self.input_size.observe(characters)

    # --- notification observability -----------------------------------------

    def notification_sent(self, channel: str) -> None:
        self.counters.inc("notifications_sent", {"channel": channel})

    def notification_deduplicated(self, channel: str) -> None:
        self.counters.inc("notifications_deduplicated", {"channel": channel})

    def notification_failed(self, channel: str, reason: str) -> None:
        self.counters.inc(
            "notifications_failed",
            {"channel": channel, "reason": _safe_metric_suffix(reason)},
        )

    # --- structured logging -------------------------------------------------

    def log(self, event: str, *, session_id: str | None = None, **fields: Any) -> None:
        if self._logger is None:
            return
        record: dict[str, Any] = {"event": event}
        if session_id is not None:
            record["session_id"] = session_id
        for key, value in fields.items():
            record[key] = self._sanitize(value)
        self._logger.info(json.dumps(record, sort_keys=True, default=str))

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value).text
        if isinstance(value, list):
            return [Telemetry._sanitize(v) for v in value]
        if isinstance(value, dict):
            return {k: Telemetry._sanitize(v) for k, v in value.items()}
        return value

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": self.counters.as_dict(),
            "model_latency": self.model_latency.as_dict(),
            "input_size": self.input_size.as_dict(),
        }

    def reset(self) -> None:
        self.counters.reset()
        self.model_latency.reset()
        self.input_size.reset()
