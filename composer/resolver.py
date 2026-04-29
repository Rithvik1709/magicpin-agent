"""
Context resolver: trigger → merchant → category → customer (optional).

Given a trigger_id, resolves the full 4-context tuple needed for composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .context_store import ContextStore


@dataclass
class ResolvedContext:
    """The fully-resolved 4-context tuple for composition."""
    category: dict
    merchant: dict
    trigger: dict
    customer: Optional[dict]
    # IDs for traceability
    trigger_id: str
    merchant_id: str
    customer_id: Optional[str]
    category_slug: str


class ContextResolver:
    """Resolves trigger_id → full context tuple."""

    def __init__(self, store: ContextStore):
        self.store = store

    def resolve(self, trigger_id: str) -> Optional[ResolvedContext]:
        """
        Resolve a trigger to its full context tuple.

        Returns None if any required context is missing (trigger, merchant, or category).
        """
        trigger = self.store.get_trigger(trigger_id)
        if not trigger:
            return None

        merchant_id = trigger.get("merchant_id")
        if not merchant_id:
            return None

        merchant = self.store.get_merchant(merchant_id)
        if not merchant:
            return None

        category_slug = merchant.get("category_slug")
        if not category_slug:
            return None

        category = self.store.get_category(category_slug)
        if not category:
            return None

        # Customer is optional — only for customer-scope triggers
        customer_id = trigger.get("customer_id")
        customer = self.store.get_customer(customer_id) if customer_id else None

        return ResolvedContext(
            category=category,
            merchant=merchant,
            trigger=trigger,
            customer=customer,
            trigger_id=trigger_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            category_slug=category_slug,
        )
