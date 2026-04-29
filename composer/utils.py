"""
Utility helpers for Vera bot (logging sanitization, helpers shared across modules).
"""
from __future__ import annotations

from typing import Any

REDACT_KEYS = {"llm_api_key", "api_key", "password", "secret", "token", "LLM_API_KEY"}


def sanitize_for_logs(obj: Any) -> Any:
    """Return a copy of obj with sensitive keys redacted for safe logging.

    Works for nested dicts and lists. Other types are returned unchanged.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in REDACT_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = sanitize_for_logs(v)
        return out
    if isinstance(obj, list):
        return [sanitize_for_logs(v) for v in obj]
    return obj
