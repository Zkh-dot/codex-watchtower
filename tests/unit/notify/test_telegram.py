from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx

from codex_watchtower.notify.telegram import (
    DEFAULT_MAX_RETRIES,
    PendingDelivery,
    TelegramNotifier,
    compute_backoff_seconds,
    process_delivery,
)

BOT_TOKEN = "123456:ABC-DEF-fake-token"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
NOW = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)


def _notifier() -> TelegramNotifier:
    return TelegramNotifier(bot_token=BOT_TOKEN, chat_id_allowlist=["12345"])


# --- successful, rate-limited, transient, permanent responses ------------


@respx.mock
def test_successful_send() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    result = _notifier().send("12345", "hello")
    assert result.delivered is True


@respx.mock
def test_rate_limited_response_carries_retry_after() -> None:
    respx.post(API_URL).mock(
        return_value=httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
    )
    result = _notifier().send("12345", "hello")
    assert result.delivered is False
    assert result.permanent_failure is False
    assert result.retry_after_seconds == 7.0


@respx.mock
def test_transient_5xx_is_retryable() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(503))
    result = _notifier().send("12345", "hello")
    assert result.delivered is False
    assert result.permanent_failure is False


@respx.mock
def test_permanent_4xx_is_not_retryable() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(400, json={"ok": False}))
    result = _notifier().send("12345", "hello")
    assert result.delivered is False
    assert result.permanent_failure is True


@respx.mock
def test_network_error_is_retryable() -> None:
    respx.post(API_URL).mock(side_effect=httpx.ConnectError("refused"))
    result = _notifier().send("12345", "hello")
    assert result.delivered is False
    assert result.permanent_failure is False


def test_chat_id_not_in_allowlist_is_rejected() -> None:
    notifier = _notifier()
    try:
        notifier.send("99999", "hello")
        raise AssertionError("expected ValueError for disallowed chat_id")
    except ValueError:
        pass


# --- delivery/retry state, not the token ------------------------------


def test_pending_delivery_carries_no_token_field() -> None:
    fields = set(PendingDelivery.__dataclass_fields__.keys())
    assert "bot_token" not in fields
    assert "token" not in fields
    assert fields == {"dedup_key", "chat_id", "text", "attempt", "next_retry_at"}


# --- exponential backoff with retry_after support -------------------------


def test_backoff_grows_exponentially_without_retry_after() -> None:
    b0 = compute_backoff_seconds(0)
    b1 = compute_backoff_seconds(1)
    b2 = compute_backoff_seconds(2)
    assert b1 > b0
    assert b2 > b1


def test_backoff_capped_at_maximum() -> None:
    assert compute_backoff_seconds(20, maximum=300.0) == 300.0


def test_backoff_respects_telegram_retry_after() -> None:
    assert compute_backoff_seconds(0, retry_after=45.0) == 45.0


def test_backoff_retry_after_still_capped() -> None:
    assert compute_backoff_seconds(0, retry_after=10_000.0, maximum=300.0) == 300.0


# --- mark sent only after confirmed API success ---------------------------


@respx.mock
def test_process_delivery_marks_delivered_only_on_confirmed_success() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    pending = PendingDelivery(dedup_key="k1", chat_id="12345", text="hello")
    outcome = process_delivery(_notifier(), pending, now=NOW)
    assert outcome.delivered is True
    assert outcome.pending is None


@respx.mock
def test_process_delivery_not_delivered_on_transient_failure_schedules_retry() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(503))
    pending = PendingDelivery(dedup_key="k1", chat_id="12345", text="hello")
    outcome = process_delivery(_notifier(), pending, now=NOW)
    assert outcome.delivered is False
    assert outcome.gave_up is False
    assert outcome.pending is not None
    assert outcome.pending.attempt == 1
    assert outcome.pending.next_retry_at is not None
    assert outcome.pending.next_retry_at > NOW


@respx.mock
def test_process_delivery_gives_up_on_permanent_failure() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(400, json={"ok": False}))
    pending = PendingDelivery(dedup_key="k1", chat_id="12345", text="hello")
    outcome = process_delivery(_notifier(), pending, now=NOW)
    assert outcome.delivered is False
    assert outcome.gave_up is True
    assert outcome.pending is None


@respx.mock
def test_process_delivery_gives_up_after_max_retries() -> None:
    respx.post(API_URL).mock(return_value=httpx.Response(503))
    pending = PendingDelivery(
        dedup_key="k1", chat_id="12345", text="hello", attempt=DEFAULT_MAX_RETRIES - 1
    )
    outcome = process_delivery(_notifier(), pending, now=NOW)
    assert outcome.delivered is False
    assert outcome.gave_up is True
    assert outcome.pending is None


def test_process_delivery_not_yet_due_does_not_attempt(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If next_retry_at is in the future, no HTTP call should be made at all."""
    called = False

    class ExplodingClient:
        def post(self, *args: object, **kwargs: object) -> None:
            nonlocal called
            called = True
            raise AssertionError("should not have sent a request before the retry time")

    notifier = TelegramNotifier(
        bot_token=BOT_TOKEN,
        chat_id_allowlist=["12345"],
        http_client=ExplodingClient(),  # type: ignore[arg-type]
    )
    pending = PendingDelivery(
        dedup_key="k1", chat_id="12345", text="hello", next_retry_at=NOW + timedelta(minutes=5)
    )
    outcome = process_delivery(notifier, pending, now=NOW)
    assert called is False
    assert outcome.pending == pending
