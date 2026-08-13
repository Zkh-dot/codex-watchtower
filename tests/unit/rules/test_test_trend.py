from __future__ import annotations

from codex_watchtower import domain
from codex_watchtower.rules.tests import (
    TestCounts,
    Trend,
    classify_trend,
    detect_test_regression,
    extract_test_counts,
    extract_test_counts_from_event,
)


def _test_event(idx: int, summary: str, exit_code: int | None = None) -> domain.Event:
    return domain.Event(
        id=f"evt:test:{idx}",
        timestamp="2026-08-13T10:00:00Z",
        kind=domain.EventKind.test_result,
        summary=summary,
        exit_code=exit_code,
    )


# --- extraction across runners -------------------------------------------


def test_pytest_style_failed_and_passed() -> None:
    counts = extract_test_counts("3 failed, 5 passed in 1.23s")
    assert counts == TestCounts(passed=5, failed=3)


def test_pytest_style_only_passed() -> None:
    counts = extract_test_counts("5 passed in 0.50s")
    assert counts == TestCounts(passed=5, failed=None)


def test_cargo_test_style() -> None:
    counts = extract_test_counts("test result: FAILED. 5 passed; 3 failed; 0 ignored")
    assert counts.passed == 5
    assert counts.failed == 3


def test_vitest_jest_style() -> None:
    counts = extract_test_counts("Tests: 2 failed, 8 passed")
    assert counts.passed == 8
    assert counts.failed == 2


def test_unparseable_summary_returns_none_counts() -> None:
    counts = extract_test_counts("ran the test suite, results attached")
    assert counts == TestCounts(passed=None, failed=None)


def test_generic_exit_code_zero_fallback() -> None:
    event = _test_event(0, "ran the test suite", exit_code=0)
    counts = extract_test_counts_from_event(event)
    assert counts.failed == 0


def test_generic_exit_code_nonzero_is_unknown_magnitude() -> None:
    event = _test_event(0, "ran the test suite", exit_code=1)
    counts = extract_test_counts_from_event(event)
    assert counts.failed is None


# --- trend classification ------------------------------------------------


def test_improving_trend() -> None:
    assert (
        classify_trend(TestCounts(passed=5, failed=3), TestCounts(passed=7, failed=1))
        == Trend.improving
    )


def test_stable_trend() -> None:
    assert (
        classify_trend(TestCounts(passed=5, failed=3), TestCounts(passed=5, failed=3))
        == Trend.stable
    )


def test_worsening_trend() -> None:
    assert (
        classify_trend(TestCounts(passed=5, failed=1), TestCounts(passed=3, failed=3))
        == Trend.worsening
    )


def test_unknown_trend_when_either_side_unparseable() -> None:
    assert classify_trend(TestCounts(None, None), TestCounts(passed=5, failed=0)) == Trend.unknown
    assert classify_trend(TestCounts(passed=5, failed=0), TestCounts(None, None)) == Trend.unknown


# --- regression signal ----------------------------------------------------


def test_worsening_test_result_emits_regression_signal() -> None:
    events = [
        _test_event(0, "1 failed, 5 passed"),
        _test_event(1, "3 failed, 3 passed"),
    ]
    signal = detect_test_regression(events)
    assert signal is not None
    assert signal.kind == domain.SignalKind.test_regression
    assert signal.severity == domain.Severity.warning


def test_improving_test_result_emits_no_signal() -> None:
    events = [
        _test_event(0, "3 failed, 3 passed"),
        _test_event(1, "0 failed, 6 passed"),
    ]
    assert detect_test_regression(events) is None


def test_single_test_result_emits_no_signal() -> None:
    events = [_test_event(0, "1 failed, 5 passed")]
    assert detect_test_regression(events) is None
