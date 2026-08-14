"""Evaluation metrics for the Luna/Terra assessment cascade (spec section 11).

These metrics are computed from a labeled evaluation corpus to determine
whether the cascade earns its complexity. The unit-testable parts are the
metric computations themselves; the full evaluation harness
(``evaluation/run.py``) requires a labeled corpus and model endpoints.

Macro-F1, precision, and recall are computed per class and then averaged
equally (macro), so rare classes are not drowned out by common ones. The
ambiguous-case threshold (spec 11: "refuse to emit a percentage-point
verdict below 30 ambiguous cases") is enforced in the harness, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConfusionEntry:
    predicted: str
    actual: str
    count: int


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    label: str
    precision: float
    recall: float
    f1: float
    support: int  # number of actual instances


@dataclass(frozen=True, slots=True)
class ClassificationReport:
    classes: list[ClassMetrics]
    macro_f1: float
    macro_precision: float
    macro_recall: float
    total_samples: int


def compute_classification(
    predictions: list[str],
    labels: list[str],
    class_names: list[str] | None = None,
) -> ClassificationReport:
    if len(predictions) != len(labels):
        raise ValueError(
            f"predictions ({len(predictions)}) and labels ({len(labels)}) must have equal length"
        )
    if not labels:
        raise ValueError("at least one sample is required")

    if class_names is None:
        class_names = sorted(set(labels) | set(predictions))

    class_metrics: list[ClassMetrics] = []
    for label in class_names:
        tp = sum(1 for p, a in zip(predictions, labels, strict=True) if p == label and a == label)
        fp = sum(1 for p, a in zip(predictions, labels, strict=True) if p == label and a != label)
        fn = sum(1 for p, a in zip(predictions, labels, strict=True) if p != label and a == label)
        support = sum(1 for a in labels if a == label)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        class_metrics.append(
            ClassMetrics(label=label, precision=precision, recall=recall, f1=f1, support=support)
        )

    macro_f1 = sum(c.f1 for c in class_metrics) / len(class_metrics) if class_metrics else 0.0
    macro_precision = (
        sum(c.precision for c in class_metrics) / len(class_metrics) if class_metrics else 0.0
    )
    macro_recall = (
        sum(c.recall for c in class_metrics) / len(class_metrics) if class_metrics else 0.0
    )

    return ClassificationReport(
        classes=class_metrics,
        macro_f1=macro_f1,
        macro_precision=macro_precision,
        macro_recall=macro_recall,
        total_samples=len(labels),
    )


@dataclass(frozen=True, slots=True)
class AttentionMetrics:
    precision: float
    recall: float
    false_attention_rate: float
    total_predicted: int
    total_actual: int
    true_positives: int
    false_positives: int
    false_negatives: int


def compute_attention(
    predicted_attention: list[bool],
    actual_attention: list[bool],
) -> AttentionMetrics:
    if len(predicted_attention) != len(actual_attention):
        raise ValueError("predicted and actual attention lists must have equal length")

    tp = sum(1 for p, a in zip(predicted_attention, actual_attention, strict=True) if p and a)
    fp = sum(1 for p, a in zip(predicted_attention, actual_attention, strict=True) if p and not a)
    fn = sum(1 for p, a in zip(predicted_attention, actual_attention, strict=True) if not p and a)

    total_predicted = sum(1 for p in predicted_attention if p)
    total_actual = sum(1 for a in actual_attention if a)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    false_attention_rate = fp / total_predicted if total_predicted > 0 else 0.0

    return AttentionMetrics(
        precision=precision,
        recall=recall,
        false_attention_rate=false_attention_rate,
        total_predicted=total_predicted,
        total_actual=total_actual,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
    )


@dataclass(frozen=True, slots=True)
class CitationReport:
    total_assessments: int
    citations_valid: int
    citations_invalid: int
    validity_rate: float


def compute_citation_validity(
    unresolved_counts: list[int],
) -> CitationReport:
    total = len(unresolved_counts)
    if total == 0:
        raise ValueError("at least one assessment is required")
    valid = sum(1 for c in unresolved_counts if c == 0)
    invalid = total - valid
    return CitationReport(
        total_assessments=total,
        citations_valid=valid,
        citations_invalid=invalid,
        validity_rate=valid / total,
    )


@dataclass(frozen=True, slots=True)
class LatencyReport:
    min_ms: float
    max_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    count: int


def compute_latency(latencies_ms: list[float]) -> LatencyReport:
    if not latencies_ms:
        raise ValueError("at least one latency sample is required")
    sorted_lat = sorted(latencies_ms)
    n = len(sorted_lat)

    def _percentile(p: float) -> float:
        idx = max(0, min(n - 1, int(round(p * (n - 1)))))
        return sorted_lat[idx]

    return LatencyReport(
        min_ms=sorted_lat[0],
        max_ms=sorted_lat[-1],
        mean_ms=sum(sorted_lat) / n,
        p50_ms=_percentile(0.50),
        p95_ms=_percentile(0.95),
        count=n,
    )


@dataclass(frozen=True, slots=True)
class TokenReport:
    total_input_tokens: int
    total_output_tokens: int
    mean_input_per_call: float
    mean_output_per_call: float
    call_count: int


def compute_tokens(input_tokens: list[int], output_tokens: list[int]) -> TokenReport:
    if len(input_tokens) != len(output_tokens):
        raise ValueError("input and output token lists must have equal length")
    if not input_tokens:
        raise ValueError("at least one call is required")
    n = len(input_tokens)
    return TokenReport(
        total_input_tokens=sum(input_tokens),
        total_output_tokens=sum(output_tokens),
        mean_input_per_call=sum(input_tokens) / n,
        mean_output_per_call=sum(output_tokens) / n,
        call_count=n,
    )


@dataclass
class EvaluationResult:
    classification: ClassificationReport
    attention: AttentionMetrics
    citation: CitationReport
    latency: LatencyReport
    tokens: TokenReport
    ambiguous_case_count: int
    extra: dict[str, str] = field(default_factory=dict)

    def refusal_triggered(self, threshold: int = 30) -> bool:
        return self.ambiguous_case_count < threshold
