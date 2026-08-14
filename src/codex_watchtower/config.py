"""Validated Watchtower configuration (spec 5.6, 7.1/7.3, 9, 12).

Safe defaults throughout: local-only API bind, Telegram off, AgentLens
optional/off, and remote model trust is never assumed -- a model profile
defaults to ``local`` trust and moving it to ``trusted-remote`` requires
both an HTTPS endpoint and explicit recorded operator consent.

Model identifiers are deployment configuration (``ModelEndpointConfig.
model_identifier``/``profile_name``), never hard-coded protocol constants --
spikes/models/README.md records that no live deployment was available to
resolve real identifiers in this environment.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codex_watchtower.models.config import ModelProfile

MIN_MAX_RESPONSE_BYTES = 64 * 1024
MAX_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_PACKET_BUDGET_CHARACTERS = 4_000  # below this, no observation could carry a useful window
MAX_PACKET_BUDGET_CHARACTERS = 10_000_000  # the schema's own finiteness ceiling


class ConfigError(ValueError):
    """Raised for an invalid configuration; startup must fail closed, not guess."""


class TrustMode(StrEnum):
    local = "local"
    trusted_remote = "trusted-remote"
    metadata_only = "metadata-only"


@dataclass(frozen=True, slots=True)
class BindConfig:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    enabled: bool = False
    bot_token_env_var: str | None = None
    chat_id_allowlist: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AgentLensConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:4316/mcp"


@dataclass(frozen=True, slots=True)
class RemoteTransportPolicy:
    """spec 7.3: HTTPS-only, no redirects, no link-local/loopback/metadata destinations,
    no inherited proxy environment, by default -- for *remote* trust mode only."""

    allow_redirects: bool = False
    trust_environment_proxy: bool = False
    reject_loopback_and_link_local: bool = True
    reject_cloud_metadata_addresses: bool = True


@dataclass(frozen=True, slots=True)
class ModelEndpointConfig:
    profile_name: str
    endpoint: str
    model_identifier: str
    trust_mode: TrustMode = TrustMode.local
    api_key_env_var: str | None = None
    max_response_bytes: int = 1_048_576
    retry_budget: int = 1
    remote_consent_given: bool = False
    remote_transport_policy: RemoteTransportPolicy = field(default_factory=RemoteTransportPolicy)
    provider_retention_policy_note: str | None = None

    def to_model_profile(self) -> ModelProfile:
        """Bridge to models.config.ModelProfile, the client's own transport-level type.

        Kept as a separate type deliberately: this class is the
        operator-facing, validated *configuration* schema (trust mode,
        consent, transport policy); ModelProfile is the narrower shape
        models/client.py actually needs to build a request.
        """
        from codex_watchtower.models.config import ModelProfile

        return ModelProfile(
            name=self.profile_name,
            endpoint=self.endpoint,
            model_identifier=self.model_identifier,
            api_key_env_var=self.api_key_env_var,
            max_response_bytes=self.max_response_bytes,
            retry_budget=self.retry_budget,
            trust_env=self.remote_transport_policy.trust_environment_proxy,
            follow_redirects=self.remote_transport_policy.allow_redirects,
        )


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    packet_character_budget: int = 48_000
    per_session_assessment_ceiling: int | None = None
    daily_cost_ceiling_cents: int | None = None


@dataclass(frozen=True, slots=True)
class WatchtowerConfig:
    sessions_root: Path
    state_dir: Path
    bind: BindConfig = field(default_factory=BindConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    agentlens: AgentLensConfig = field(default_factory=AgentLensConfig)
    luna: ModelEndpointConfig | None = None
    terra: ModelEndpointConfig | None = None
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    operator_token: str | None = None


_UNSAFE_HOST_PREFIXES = ("127.", "169.254.", "10.", "172.16.", "192.168.")
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}


def validate_remote_endpoint(url: str, policy: RemoteTransportPolicy) -> None:
    """Reject an endpoint URL that spec 7.3's remote-transport policy forbids.

    A pure, directly testable function: models/client.py's remote-mode
    request path calls this once before the first transmission to a
    ``trusted-remote`` profile.
    """
    if not url.startswith("https://"):
        raise ConfigError(f"remote endpoint must use HTTPS: {url!r}")
    host = url.removeprefix("https://").split("/", 1)[0].split(":", 1)[0]
    if policy.reject_cloud_metadata_addresses and host in _METADATA_HOSTS:
        raise ConfigError(f"remote endpoint must not be a cloud metadata address: {url!r}")
    if host == "localhost":
        raise ConfigError(f"remote endpoint must not be loopback/link-local: {url!r}")
    if policy.reject_loopback_and_link_local and any(
        host.startswith(p) for p in _UNSAFE_HOST_PREFIXES
    ):
        raise ConfigError(f"remote endpoint must not be loopback/link-local: {url!r}")


def _validate_model(name: str, model: ModelEndpointConfig | None) -> None:
    if model is None:
        return
    if not (MIN_MAX_RESPONSE_BYTES <= model.max_response_bytes <= MAX_MAX_RESPONSE_BYTES):
        raise ConfigError(
            f"{name}.max_response_bytes must be within "
            f"[{MIN_MAX_RESPONSE_BYTES}, {MAX_MAX_RESPONSE_BYTES}], "
            f"got {model.max_response_bytes}"
        )
    if model.retry_budget < 0:
        raise ConfigError(f"{name}.retry_budget must be >= 0")
    if model.trust_mode == TrustMode.trusted_remote:
        if not model.remote_consent_given:
            raise ConfigError(
                f"{name} uses trust_mode=trusted-remote but remote_consent_given is not set; "
                "explicit operator consent is required before the first remote transmission"
            )
        validate_remote_endpoint(model.endpoint, model.remote_transport_policy)


def validate_config(config: WatchtowerConfig) -> None:
    """Raise ConfigError for any invalid combination. Called once at startup."""
    budget = config.budget
    packet_budget = budget.packet_character_budget
    if not (MIN_PACKET_BUDGET_CHARACTERS <= packet_budget <= MAX_PACKET_BUDGET_CHARACTERS):
        raise ConfigError(
            f"budget.packet_character_budget must be within "
            f"[{MIN_PACKET_BUDGET_CHARACTERS}, {MAX_PACKET_BUDGET_CHARACTERS}]"
        )
    ceiling = budget.per_session_assessment_ceiling
    if ceiling is not None and ceiling < 1:
        raise ConfigError("budget.per_session_assessment_ceiling must be >= 1 when set")
    cost_ceiling = budget.daily_cost_ceiling_cents
    if cost_ceiling is not None and cost_ceiling < 0:
        raise ConfigError("budget.daily_cost_ceiling_cents must be >= 0 when set")

    _validate_model("luna", config.luna)
    _validate_model("terra", config.terra)

    if config.telegram.enabled:
        if not config.telegram.bot_token_env_var:
            raise ConfigError("telegram.enabled requires bot_token_env_var")
        if not config.telegram.chat_id_allowlist:
            raise ConfigError("telegram.enabled requires a non-empty chat_id_allowlist")

    if config.bind.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(
            f"bind.host must be a loopback address by default, got {config.bind.host!r}; "
            "widening the bind is an explicit, documented operator choice, not a default"
        )


def _model_from_dict(name: str, data: dict[str, Any] | None) -> ModelEndpointConfig | None:
    if data is None:
        return None
    policy_data = data.get("remote_transport_policy", {})
    return ModelEndpointConfig(
        profile_name=data.get("profile_name", name),
        endpoint=data["endpoint"],
        model_identifier=data["model_identifier"],
        trust_mode=TrustMode(data.get("trust_mode", "local")),
        api_key_env_var=data.get("api_key_env_var"),
        max_response_bytes=data.get("max_response_bytes", 1_048_576),
        retry_budget=data.get("retry_budget", 1),
        remote_consent_given=data.get("remote_consent_given", False),
        remote_transport_policy=RemoteTransportPolicy(
            allow_redirects=policy_data.get("allow_redirects", False),
            trust_environment_proxy=policy_data.get("trust_environment_proxy", False),
            reject_loopback_and_link_local=policy_data.get("reject_loopback_and_link_local", True),
            reject_cloud_metadata_addresses=policy_data.get(
                "reject_cloud_metadata_addresses", True
            ),
        ),
        provider_retention_policy_note=data.get("provider_retention_policy_note"),
    )


def config_from_dict(data: dict[str, Any]) -> WatchtowerConfig:
    bind_data = data.get("bind", {})
    telegram_data = data.get("telegram", {})
    agentlens_data = data.get("agentlens", {})
    budget_data = data.get("budget", {})
    return WatchtowerConfig(
        sessions_root=Path(data["sessions_root"]).expanduser(),
        state_dir=Path(data["state_dir"]).expanduser(),
        bind=BindConfig(host=bind_data.get("host", "127.0.0.1"), port=bind_data.get("port", 8787)),
        telegram=TelegramConfig(
            enabled=telegram_data.get("enabled", False),
            bot_token_env_var=telegram_data.get("bot_token_env_var"),
            chat_id_allowlist=list(telegram_data.get("chat_id_allowlist", [])),
        ),
        agentlens=AgentLensConfig(
            enabled=agentlens_data.get("enabled", False),
            base_url=agentlens_data.get("base_url", "http://127.0.0.1:4316/mcp"),
        ),
        luna=_model_from_dict("luna", data.get("luna")),
        terra=_model_from_dict("terra", data.get("terra")),
        budget=BudgetConfig(
            packet_character_budget=budget_data.get("packet_character_budget", 48_000),
            per_session_assessment_ceiling=budget_data.get("per_session_assessment_ceiling"),
            daily_cost_ceiling_cents=budget_data.get("daily_cost_ceiling_cents"),
        ),
        operator_token=data.get("operator_token"),
    )


def load_config(path: Path) -> WatchtowerConfig:
    with path.open("rb") as f:
        data = tomllib.load(f)
    config = config_from_dict(data)
    validate_config(config)
    return config
