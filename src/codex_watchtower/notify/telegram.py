"""Telegram Bot API notifier: durable delivery without persisting the token (spec 5.10).

The bot token lives in configuration/environment and is passed to
``TelegramNotifier`` at construction; only delivery/retry bookkeeping
(``PendingDelivery``: dedup key, chat id, text, attempt count, next retry
time) is meant to be persisted, and that type carries no token field at
all -- a caller cannot accidentally serialize it into storage.

A message is marked sent only after Telegram's API confirms success
(``ok: true``); a 429 respects ``retry_after`` from the response body, a
5xx/network error backs off exponentially and retries, and a non-429 4xx
is permanent (retrying cannot fix a bad chat id or malformed request) and
is not retried.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import httpx

DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 300.0
DEFAULT_API_BASE = "https://api.telegram.org"


def compute_backoff_seconds(
    attempt: int,
    *,
    base: float = DEFAULT_BASE_BACKOFF_SECONDS,
    maximum: float = DEFAULT_MAX_BACKOFF_SECONDS,
    retry_after: float | None = None,
) -> float:
    if retry_after is not None:
        return min(retry_after, maximum)
    return min(base * (2.0**attempt), maximum)


@dataclass(frozen=True, slots=True)
class SendResult:
    delivered: bool
    permanent_failure: bool
    retry_after_seconds: float | None
    error: str | None


@dataclass
class TelegramNotifier:
    bot_token: str
    chat_id_allowlist: list[str]
    http_client: httpx.Client | None = None
    api_base: str = DEFAULT_API_BASE

    def _client(self) -> httpx.Client:
        return self.http_client or httpx.Client(timeout=10.0)

    def send(self, chat_id: str, text: str) -> SendResult:
        if chat_id not in self.chat_id_allowlist:
            raise ValueError(f"chat_id {chat_id!r} is not in the configured allowlist")

        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        try:
            response = self._client().post(
                url, json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
            )
        except httpx.HTTPError as exc:
            return SendResult(
                delivered=False,
                permanent_failure=False,
                retry_after_seconds=None,
                error=f"network error: {exc}",
            )

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if body.get("ok") is True:
                return SendResult(
                    delivered=True, permanent_failure=False, retry_after_seconds=None, error=None
                )
            return SendResult(
                delivered=False,
                permanent_failure=True,
                retry_after_seconds=None,
                error=f"api reported failure: {body}",
            )

        if response.status_code == 429:
            retry_after = None
            try:
                retry_after = response.json().get("parameters", {}).get("retry_after")
            except ValueError:
                pass
            return SendResult(
                delivered=False,
                permanent_failure=False,
                retry_after_seconds=float(retry_after) if retry_after is not None else None,
                error="rate_limited",
            )

        if 500 <= response.status_code < 600:
            return SendResult(
                delivered=False,
                permanent_failure=False,
                retry_after_seconds=None,
                error=f"transient HTTP {response.status_code}",
            )

        return SendResult(
            delivered=False,
            permanent_failure=True,
            retry_after_seconds=None,
            error=f"permanent HTTP {response.status_code}",
        )


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    dedup_key: str
    chat_id: str
    text: str
    attempt: int = 0
    next_retry_at: datetime | None = None  # None: ready now


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivered: bool
    gave_up: bool
    pending: PendingDelivery | None  # None iff delivered or gave_up


def process_delivery(
    notifier: TelegramNotifier,
    pending: PendingDelivery,
    *,
    now: datetime,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> DeliveryOutcome:
    if pending.next_retry_at is not None and now < pending.next_retry_at:
        return DeliveryOutcome(delivered=False, gave_up=False, pending=pending)

    result = notifier.send(pending.chat_id, pending.text)
    if result.delivered:
        return DeliveryOutcome(delivered=True, gave_up=False, pending=None)

    if result.permanent_failure:
        return DeliveryOutcome(delivered=False, gave_up=True, pending=None)

    if pending.attempt + 1 >= max_retries:
        return DeliveryOutcome(delivered=False, gave_up=True, pending=None)

    backoff = compute_backoff_seconds(pending.attempt, retry_after=result.retry_after_seconds)
    updated = replace(
        pending, attempt=pending.attempt + 1, next_retry_at=now + timedelta(seconds=backoff)
    )
    return DeliveryOutcome(delivered=False, gave_up=False, pending=updated)
