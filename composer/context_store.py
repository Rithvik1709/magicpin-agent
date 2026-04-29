"""
In-memory versioned context store.

Keyed by (scope, context_id). Stores {version, payload}.
Handles idempotent writes and version-gated atomic replace.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional


class ContextStore:
    """Thread-safe, versioned in-memory context store."""

    VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

    def __init__(self):
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ─── Write ────────────────────────────────────────────────────

    def push(
        self, scope: str, context_id: str, version: int, payload: dict
    ) -> tuple[bool, str | None, int | None]:
        """
        Store or update a context.

        Returns:
            (accepted, reason_if_rejected, current_version_if_stale)
        """
        if scope not in self.VALID_SCOPES:
            return False, "invalid_scope", None

        key = (scope, context_id)

        with self._lock:
            existing = self._data.get(key)
            if existing and existing["version"] >= version:
                return False, "stale_version", existing["version"]

            self._data[key] = {
                "version": version,
                "payload": payload,
                "stored_at": datetime.now(timezone.utc).isoformat(),
            }
            return True, None, None

    # ─── Read helpers ─────────────────────────────────────────────

    def get(self, scope: str, context_id: str) -> Optional[dict]:
        """Return the payload for a (scope, context_id), or None."""
        entry = self._data.get((scope, context_id))
        return entry["payload"] if entry else None

    def get_category(self, slug: str) -> Optional[dict]:
        return self.get("category", slug)

    def get_merchant(self, merchant_id: str) -> Optional[dict]:
        return self.get("merchant", merchant_id)

    def get_trigger(self, trigger_id: str) -> Optional[dict]:
        return self.get("trigger", trigger_id)

    def get_customer(self, customer_id: str) -> Optional[dict]:
        return self.get("customer", customer_id)

    # ─── Counts ───────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        """Return counts per scope for healthz."""
        result = {s: 0 for s in self.VALID_SCOPES}
        for (scope, _) in self._data:
            result[scope] = result.get(scope, 0) + 1
        return result

    # ─── Iteration helpers ────────────────────────────────────────

    def all_triggers(self) -> list[tuple[str, dict]]:
        """Return all (trigger_id, payload) pairs."""
        return [
            (cid, entry["payload"])
            for (scope, cid), entry in self._data.items()
            if scope == "trigger"
        ]

    def all_merchants(self) -> list[tuple[str, dict]]:
        """Return all (merchant_id, payload) pairs."""
        return [
            (cid, entry["payload"])
            for (scope, cid), entry in self._data.items()
            if scope == "merchant"
        ]
