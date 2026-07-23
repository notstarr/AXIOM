from __future__ import annotations

from axiom.dispatcher.redaction import redact_sensitive_text


def test_redaction_is_idempotent_for_existing_markers() -> None:
    value = "MAGIC_AMAP_KEY: [REDACTED]\nAuthorization: [REDACTED]\nJWT=[REDACTED_JWT]"
    once = redact_sensitive_text(value)
    twice = redact_sensitive_text(once)
    assert once == twice
    assert "[REDACTED]]" not in twice


def test_redaction_scrubs_key_value_without_damaging_marker() -> None:
    original = "api_key: actual-secret-value\norganization-code=secret-org"
    redacted = redact_sensitive_text(original)
    assert redacted == "api_key: [REDACTED]\norganization-code=[REDACTED]"
    assert redact_sensitive_text(redacted) == redacted


def test_redaction_scrubs_realistic_json_jwt_cookie_and_query_values() -> None:
    jwt = "eyJ" + "a" * 40 + "." + "b" * 40 + "." + "c" * 20
    original = (
        '{"access_token":"' + jwt + '","refresh_token":"refresh-secret-value",'
        '"Cookie":"sid=private-cookie; theme=dark"}\n'
        "https://example.test/api?access_token=" + jwt + "&page=1\n"
        "https://user:private-password@example.test/path"
    )
    redacted = redact_sensitive_text(original)
    assert jwt not in redacted
    assert "refresh-secret-value" not in redacted
    assert "private-cookie" not in redacted
    assert "private-password" not in redacted
    assert "access_token=[REDACTED]" in redacted
    assert redact_sensitive_text(redacted) == redacted


def test_redaction_scrubs_shell_assignments_and_header_variants() -> None:
    original = (
        "AUTH_TOKEN=private-token-value\n"
        "x-api-key: private-api-key\n"
        "curl -H 'Authorization: Bearer private-bearer-value' "
        "https://example.test"
    )
    redacted = redact_sensitive_text(original)
    assert "private-token-value" not in redacted
    assert "private-api-key" not in redacted
    assert "private-bearer-value" not in redacted
