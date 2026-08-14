"""Notification transition policy: dedup key, cooldown resend, digest (spec 5.10).

The dedup key is ``session_id + status_epoch + attention_epoch +
notification_status + signal_fingerprint``. It deliberately excludes the
event cursor (which advances on every ingested event and would make the
key unique per assessment, suppressing no duplicate at all and re-sending
after a restart that replays the same window under a fresh cursor) and is
computed purely from ``notification_status``/deterministic signals, never
from ``model_assessment`` -- so wording-only prose changes leave the key
unchanged and never resend on their own.

A *new* key (no prior delivery under it) is this episode's first
appearance. It sends only when the episode is one spec 5.10 lists:
``notification_status`` entering a tracked state, or ``needs_attention``
transitioning false->true (which is not always the same event: a critical
signal outside the kinds that narrow notification_status can raise
attention while notification_status stays ``progressing``). A *repeat* key
(already delivered) sends again only on the critical-signal cooldown or a
due periodic digest -- this is what makes wording-only and duplicate-signal
reconciliations produce no repeat send.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from codex_watchtower import domain

DEFAULT_CRITICAL_COOLDOWN = timedelta(minutes=30)

SEND_WORTHY_NOTIFICATION_STATUSES = frozenset(
    {
        domain.NotificationStatus.waiting,
        domain.NotificationStatus.stalled,
        domain.NotificationStatus.looping,
        domain.NotificationStatus.off_scope,
        domain.NotificationStatus.terminal_failed,
        domain.NotificationStatus.terminal_completed,
        domain.NotificationStatus.idle,
        domain.NotificationStatus.identity_broken,
    }
)


def deduplication_key(reconciled: domain.ReconciledAssessment) -> str:
    return (
        f"{reconciled.session_id}:{reconciled.status_epoch}:{reconciled.attention_epoch}:"
        f"{reconciled.notification_status.value}:{reconciled.signal_fingerprint}"
    )


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)


@dataclass(frozen=True, slots=True)
class LastDelivery:
    last_sent_at: str


@dataclass(frozen=True, slots=True)
class SendDecision:
    should_send: bool
    reason: str | None  # None when should_send is False


def should_send(
    reconciled: domain.ReconciledAssessment,
    *,
    last_delivery: LastDelivery | None,
    now: datetime,
    critical_cooldown: timedelta = DEFAULT_CRITICAL_COOLDOWN,
    digest_due: bool = False,
) -> SendDecision:
    if last_delivery is None:
        if reconciled.notification_status in SEND_WORTHY_NOTIFICATION_STATUSES:
            return SendDecision(True, "notification_status_entered")
        if reconciled.needs_attention:
            return SendDecision(True, "attention_transition")
        return SendDecision(False, None)

    has_critical = any(s.severity == domain.Severity.critical for s in reconciled.active_signals)
    if has_critical and (now - _parse(last_delivery.last_sent_at)) >= critical_cooldown:
        return SendDecision(True, "critical_cooldown_resend")
    if digest_due:
        return SendDecision(True, "digest")
    return SendDecision(False, None)
