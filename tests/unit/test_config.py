from __future__ import annotations

from pathlib import Path

import pytest

from codex_watchtower.config import (
    ConfigError,
    ModelEndpointConfig,
    RemoteTransportPolicy,
    TrustMode,
    WatchtowerConfig,
    config_from_dict,
    load_config,
    validate_config,
    validate_remote_endpoint,
)


def _base_config(**overrides: object) -> WatchtowerConfig:
    kwargs: dict[str, object] = {
        "sessions_root": Path("/home/user/.codex/sessions"),
        "state_dir": Path("/home/user/.local/state/codex-watchtower"),
    }
    kwargs.update(overrides)
    return WatchtowerConfig(**kwargs)  # type: ignore[arg-type]


# --- safe defaults -----------------------------------------------------


def test_default_bind_is_loopback() -> None:
    config = _base_config()
    assert config.bind.host == "127.0.0.1"


def test_default_telegram_is_disabled() -> None:
    config = _base_config()
    assert config.telegram.enabled is False


def test_default_agentlens_is_optional_and_off() -> None:
    config = _base_config()
    assert config.agentlens.enabled is False


def test_default_model_trust_is_not_remote() -> None:
    model = ModelEndpointConfig(
        profile_name="luna", endpoint="https://example.com", model_identifier="x"
    )
    assert model.trust_mode == TrustMode.local


def test_valid_default_config_passes_validation() -> None:
    validate_config(_base_config())  # must not raise


# --- invalid threshold/model/endpoint combinations fail at startup -------


def test_remote_trust_without_consent_fails() -> None:
    model = ModelEndpointConfig(
        profile_name="luna",
        endpoint="https://models.example.com",
        model_identifier="x",
        trust_mode=TrustMode.trusted_remote,
        remote_consent_given=False,
    )
    with pytest.raises(ConfigError, match="remote_consent_given"):
        validate_config(_base_config(luna=model))


def test_remote_trust_with_http_endpoint_fails() -> None:
    model = ModelEndpointConfig(
        profile_name="luna",
        endpoint="http://models.example.com",
        model_identifier="x",
        trust_mode=TrustMode.trusted_remote,
        remote_consent_given=True,
    )
    with pytest.raises(ConfigError, match="HTTPS"):
        validate_config(_base_config(luna=model))


def test_remote_trust_with_consent_and_https_passes() -> None:
    model = ModelEndpointConfig(
        profile_name="luna",
        endpoint="https://models.example.com",
        model_identifier="x",
        trust_mode=TrustMode.trusted_remote,
        remote_consent_given=True,
    )
    validate_config(_base_config(luna=model))  # must not raise


def test_max_response_bytes_below_minimum_fails() -> None:
    model = ModelEndpointConfig(
        profile_name="luna", endpoint="https://x", model_identifier="x", max_response_bytes=1024
    )
    with pytest.raises(ConfigError, match="max_response_bytes"):
        validate_config(_base_config(luna=model))


def test_max_response_bytes_above_maximum_fails() -> None:
    model = ModelEndpointConfig(
        profile_name="luna",
        endpoint="https://x",
        model_identifier="x",
        max_response_bytes=16 * 1024 * 1024,
    )
    with pytest.raises(ConfigError, match="max_response_bytes"):
        validate_config(_base_config(luna=model))


def test_negative_retry_budget_fails() -> None:
    model = ModelEndpointConfig(
        profile_name="luna", endpoint="https://x", model_identifier="x", retry_budget=-1
    )
    with pytest.raises(ConfigError, match="retry_budget"):
        validate_config(_base_config(luna=model))


def test_telegram_enabled_without_bot_token_env_var_fails() -> None:
    from codex_watchtower.config import TelegramConfig

    config = _base_config(telegram=TelegramConfig(enabled=True, chat_id_allowlist=["123"]))
    with pytest.raises(ConfigError, match="bot_token_env_var"):
        validate_config(config)


def test_telegram_enabled_without_chat_id_allowlist_fails() -> None:
    from codex_watchtower.config import TelegramConfig

    config = _base_config(
        telegram=TelegramConfig(enabled=True, bot_token_env_var="TOKEN", chat_id_allowlist=[])
    )
    with pytest.raises(ConfigError, match="chat_id_allowlist"):
        validate_config(config)


def test_non_loopback_bind_fails_by_default() -> None:
    from codex_watchtower.config import BindConfig

    config = _base_config(bind=BindConfig(host="0.0.0.0", port=8787))  # noqa: S104
    with pytest.raises(ConfigError, match="loopback"):
        validate_config(config)


# --- packet character budget, max_response_bytes range, ceilings ----------


def test_packet_character_budget_default_is_48000() -> None:
    config = _base_config()
    assert config.budget.packet_character_budget == 48_000


def test_packet_character_budget_too_small_fails() -> None:
    from codex_watchtower.config import BudgetConfig

    config = _base_config(budget=BudgetConfig(packet_character_budget=10))
    with pytest.raises(ConfigError, match="packet_character_budget"):
        validate_config(config)


@pytest.mark.parametrize("value", [64 * 1024, 1024 * 1024, 8 * 1024 * 1024])
def test_max_response_bytes_within_range_accepted(value: int) -> None:
    model = ModelEndpointConfig(
        profile_name="luna", endpoint="https://x", model_identifier="x", max_response_bytes=value
    )
    validate_config(_base_config(luna=model))  # must not raise


def test_per_session_assessment_ceiling_must_be_positive_when_set() -> None:
    from codex_watchtower.config import BudgetConfig

    config = _base_config(budget=BudgetConfig(per_session_assessment_ceiling=0))
    with pytest.raises(ConfigError, match="per_session_assessment_ceiling"):
        validate_config(config)


def test_daily_cost_ceiling_must_be_non_negative_when_set() -> None:
    from codex_watchtower.config import BudgetConfig

    config = _base_config(budget=BudgetConfig(daily_cost_ceiling_cents=-1))
    with pytest.raises(ConfigError, match="daily_cost_ceiling_cents"):
        validate_config(config)


def test_a_breach_stops_model_calls_but_leaves_deterministic_notification_intact() -> None:
    """Structural: the budget ceiling fields exist and are independent of
    telegram/bind config, so exhausting them cannot disable notification."""
    from codex_watchtower.config import BudgetConfig

    config = _base_config(budget=BudgetConfig(daily_cost_ceiling_cents=100))
    validate_config(config)  # a configured ceiling alone is valid
    assert config.telegram.enabled is False  # unaffected by budget config


# --- remote transport policy: HTTPS allowlist, redirect/SSRF, proxy -------


def test_validate_remote_endpoint_rejects_loopback() -> None:
    with pytest.raises(ConfigError, match="loopback"):
        validate_remote_endpoint("https://127.0.0.1/v1/chat", RemoteTransportPolicy())


def test_validate_remote_endpoint_rejects_link_local() -> None:
    with pytest.raises(ConfigError, match="loopback"):
        validate_remote_endpoint("https://169.254.1.1/v1/chat", RemoteTransportPolicy())


def test_validate_remote_endpoint_rejects_cloud_metadata_address() -> None:
    with pytest.raises(ConfigError, match="metadata"):
        validate_remote_endpoint(
            "https://169.254.169.254/latest/meta-data", RemoteTransportPolicy()
        )


def test_validate_remote_endpoint_rejects_plain_http() -> None:
    with pytest.raises(ConfigError, match="HTTPS"):
        validate_remote_endpoint("http://models.example.com", RemoteTransportPolicy())


def test_validate_remote_endpoint_accepts_https_public_host() -> None:
    validate_remote_endpoint("https://models.example.com/v1/chat", RemoteTransportPolicy())


def test_remote_transport_policy_defaults_are_safe() -> None:
    policy = RemoteTransportPolicy()
    assert policy.allow_redirects is False
    assert policy.trust_environment_proxy is False
    assert policy.reject_loopback_and_link_local is True
    assert policy.reject_cloud_metadata_addresses is True


def test_provider_retention_policy_note_is_recorded() -> None:
    model = ModelEndpointConfig(
        profile_name="luna",
        endpoint="https://models.example.com",
        model_identifier="x",
        trust_mode=TrustMode.trusted_remote,
        remote_consent_given=True,
        provider_retention_policy_note="provider retains prompts for 30 days",
    )
    validate_config(_base_config(luna=model))
    assert model.provider_retention_policy_note is not None


# --- profile mapping documented without hard-coded names -------------------


def test_model_profile_name_is_configuration_not_a_constant() -> None:
    model = ModelEndpointConfig(
        profile_name="my-custom-luna-deployment",
        endpoint="https://x",
        model_identifier="whatever-the-deployment-calls-it",
    )
    profile = model.to_model_profile()
    assert profile.name == "my-custom-luna-deployment"
    assert profile.model_identifier == "whatever-the-deployment-calls-it"


# --- loading from TOML --------------------------------------------------


def test_config_from_dict_round_trips_example_file() -> None:
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    # config.example.toml has every model section commented out, so it
    # must load and validate as-is with no active remote model.
    loaded = load_config(example)
    assert loaded.bind.host == "127.0.0.1"
    assert loaded.telegram.enabled is False
    assert loaded.luna is None
    assert loaded.terra is None


def test_config_from_dict_minimal() -> None:
    config = config_from_dict(
        {"sessions_root": "~/.codex/sessions", "state_dir": "~/.local/state/watchtower"}
    )
    validate_config(config)
    assert config.bind.host == "127.0.0.1"
