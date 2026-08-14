from __future__ import annotations

from codex_watchtower.privacy.redact import bound_text, redact_text


def test_bearer_token_redacted() -> None:
    result = redact_text("Authorization header used Bearer sk-abcdefghijklmnop123456")
    assert "sk-abcdefghijklmnop123456" not in result.text
    assert "bearer_token" in result.classes or "openai_api_key" in result.classes


def test_aws_key_redacted() -> None:
    result = redact_text("export AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP")
    assert "AKIAABCDEFGHIJKLMNOP" not in result.text
    assert "aws_access_key" in result.classes


def test_private_key_block_redacted() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK...\n-----END RSA PRIVATE KEY-----"
    result = redact_text(f"here is the key:\n{block}\nend")
    assert "MIIBOgIBAAJBAK" not in result.text
    assert "private_key_block" in result.classes


def test_plain_text_untouched() -> None:
    result = redact_text("Refactored the parser module, all tests green.")
    assert result.text == "Refactored the parser module, all tests green."
    assert result.classes == ()


def test_bound_text_truncates_and_reports() -> None:
    text, truncated = bound_text("x" * 100, 10)
    assert len(text) == 10
    assert truncated is True


def test_bound_text_no_truncation_when_within_limit() -> None:
    text, truncated = bound_text("short", 10)
    assert text == "short"
    assert truncated is False
