from composer.utils import sanitize_for_logs


def test_sanitize_redacts_keys():
    data = {"api_key": "secret", "nested": {"token": "t"}, "keep": 1}
    sanitized = sanitize_for_logs(data)
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["token"] == "[REDACTED]"
    assert sanitized["keep"] == 1
