"""Unit tests for evaluation metrics (Task 33).

Tests the metric computations (macro-F1, attention precision/recall,
false-attention rate, citation validity, latency, token metrics) without
requiring a labeled corpus or model endpoints.
"""

from __future__ import annotations

import pytest

from evaluation.metrics import (
    EvaluationResult,
    compute_attention,
    compute_citation_validity,
    compute_classification,
    compute_latency,
    compute_tokens,
)

# --- Classification (macro-F1) ----------------------------------------------


def test_perfect_classification() -> None:
    preds = ["progressing", "stalled", "looping"]
    labels = ["progressing", "stalled", "looping"]
    report = compute_classification(preds, labels)
    assert report.macro_f1 == pytest.approx(1.0)
    assert report.macro_precision == pytest.approx(1.0)
    assert report.macro_recall == pytest.approx(1.0)
    assert report.total_samples == 3


def test_imperfect_classification() -> None:
    preds = ["progressing", "stalled", "looping"]
    labels = ["progressing", "looping", "looping"]
    report = compute_classification(preds, labels)
    assert report.macro_f1 < 1.0
    looping = next(c for c in report.classes if c.label == "looping")
    assert looping.precision == pytest.approx(1.0)
    assert looping.recall == pytest.approx(0.5)
    assert looping.f1 == pytest.approx(2 * 1.0 * 0.5 / 1.5)


def test_macro_f1_weights_classes_equally() -> None:
    preds = ["progressing"] * 99 + ["stalled"]
    labels = ["progressing"] * 99 + ["progressing"]
    report = compute_classification(preds, labels)
    assert report.total_samples == 100
    prog = next(c for c in report.classes if c.label == "progressing")
    stalled = next(c for c in report.classes if c.label == "stalled")
    assert prog.support == 100
    assert stalled.support == 0
    assert stalled.f1 == 0.0
    assert report.macro_f1 < 1.0


def test_classification_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError):
        compute_classification(["a"], ["a", "b"])


def test_classification_empty_raise() -> None:
    with pytest.raises(ValueError):
        compute_classification([], [])


# --- Attention metrics -------------------------------------------------------


def test_attention_perfect() -> None:
    predicted = [True, False, True, False]
    actual = [True, False, True, False]
    m = compute_attention(predicted, actual)
    assert m.precision == pytest.approx(1.0)
    assert m.recall == pytest.approx(1.0)
    assert m.false_attention_rate == pytest.approx(0.0)
    assert m.true_positives == 2
    assert m.false_positives == 0
    assert m.false_negatives == 0


def test_attention_false_positive_rate() -> None:
    predicted = [True, True, True, False]
    actual = [True, False, False, False]
    m = compute_attention(predicted, actual)
    assert m.true_positives == 1
    assert m.false_positives == 2
    assert m.false_negatives == 0
    assert m.precision == pytest.approx(1 / 3)
    assert m.false_attention_rate == pytest.approx(2 / 3)
    assert m.recall == pytest.approx(1.0)


def test_attention_false_negative() -> None:
    predicted = [False, False, False, False]
    actual = [True, False, False, False]
    m = compute_attention(predicted, actual)
    assert m.recall == pytest.approx(0.0)
    assert m.true_positives == 0
    assert m.false_negatives == 1


def test_attention_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError):
        compute_attention([True], [True, False])


def test_attention_no_predictions() -> None:
    m = compute_attention([False, False], [True, False])
    assert m.precision == 0.0
    assert m.false_attention_rate == 0.0
    assert m.recall == 0.0


# --- Citation validity -------------------------------------------------------


def test_citation_all_valid() -> None:
    report = compute_citation_validity([0, 0, 0])
    assert report.citations_valid == 3
    assert report.citations_invalid == 0
    assert report.validity_rate == pytest.approx(1.0)


def test_citation_some_invalid() -> None:
    report = compute_citation_validity([0, 2, 0, 1])
    assert report.citations_valid == 2
    assert report.citations_invalid == 2
    assert report.validity_rate == pytest.approx(0.5)


def test_citation_empty_raise() -> None:
    with pytest.raises(ValueError):
        compute_citation_validity([])


# --- Latency -----------------------------------------------------------------


def test_latency_basic() -> None:
    report = compute_latency([100.0, 200.0, 300.0, 400.0, 500.0])
    assert report.min_ms == 100.0
    assert report.max_ms == 500.0
    assert report.mean_ms == pytest.approx(300.0)
    assert report.p50_ms == pytest.approx(300.0)
    assert report.p95_ms == pytest.approx(500.0)
    assert report.count == 5


def test_latency_single_sample() -> None:
    report = compute_latency([42.0])
    assert report.min_ms == 42.0
    assert report.max_ms == 42.0
    assert report.mean_ms == pytest.approx(42.0)
    assert report.p50_ms == pytest.approx(42.0)
    assert report.p95_ms == pytest.approx(42.0)


def test_latency_empty_raise() -> None:
    with pytest.raises(ValueError):
        compute_latency([])


# --- Token metrics -----------------------------------------------------------


def test_tokens_basic() -> None:
    report = compute_tokens([100, 200, 300], [50, 100, 150])
    assert report.total_input_tokens == 600
    assert report.total_output_tokens == 300
    assert report.mean_input_per_call == pytest.approx(200.0)
    assert report.mean_output_per_call == pytest.approx(100.0)
    assert report.call_count == 3


def test_tokens_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError):
        compute_tokens([100], [50, 60])


def test_tokens_empty_raise() -> None:
    with pytest.raises(ValueError):
        compute_tokens([], [])


# --- EvaluationResult: ambiguous-case threshold ------------------------------


def test_refusal_triggered_below_threshold() -> None:
    result = EvaluationResult(
        classification=compute_classification(["a"], ["a"]),
        attention=compute_attention([True], [True]),
        citation=compute_citation_validity([0]),
        latency=compute_latency([100.0]),
        tokens=compute_tokens([100], [50]),
        ambiguous_case_count=10,
    )
    assert result.refusal_triggered(threshold=30) is True


def test_refusal_not_triggered_at_threshold() -> None:
    result = EvaluationResult(
        classification=compute_classification(["a"] * 30, ["a"] * 30),
        attention=compute_attention([True] * 30, [True] * 30),
        citation=compute_citation_validity([0] * 30),
        latency=compute_latency([100.0]),
        tokens=compute_tokens([100], [50]),
        ambiguous_case_count=30,
    )
    assert result.refusal_triggered(threshold=30) is False
