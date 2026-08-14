"""Model deployment configuration (spec 5.7, 12).

Model identifiers and endpoints are deployment configuration, never
hard-coded protocol constants -- spikes/models/README.md records that no
live deployment was available to resolve them in this environment.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MiB
MIN_MAX_RESPONSE_BYTES = 64 * 1024
MAX_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 65536
DEFAULT_RETRY_BUDGET = 1
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """One configured model deployment (e.g. the "luna" or "terra" profile)."""

    name: str
    endpoint: str
    model_identifier: str
    api_key_env_var: str | None = None
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    chunk_size: int = DEFAULT_CHUNK_SIZE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    retry_budget: int = DEFAULT_RETRY_BUDGET
    trust_env: bool = False
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        if not (MIN_MAX_RESPONSE_BYTES <= self.max_response_bytes <= MAX_MAX_RESPONSE_BYTES):
            raise ValueError(
                f"max_response_bytes must be within "
                f"[{MIN_MAX_RESPONSE_BYTES}, {MAX_MAX_RESPONSE_BYTES}], got "
                f"{self.max_response_bytes}"
            )
        if self.retry_budget < 0:
            raise ValueError("retry_budget must be >= 0")
