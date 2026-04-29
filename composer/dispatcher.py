"""
Dispatcher: resolves context, calls LLM, validates output.

This is the main composition engine.
"""

from __future__ import annotations

import json
import re
import logging
from typing import Optional, Callable, Awaitable

from .context_store import ContextStore
from .resolver import ContextResolver, ResolvedContext
from .prompts import build_prompt
from .validator import validate

logger = logging.getLogger(__name__)


class Dispatcher:
    """Route trigger → resolve context → compose via LLM → validate."""

    def __init__(
        self,
        store: ContextStore,
        llm_fn: Callable[[str, str], Awaitable[str]],
    ):
        self.store = store
        self.resolver = ContextResolver(store)
        self.llm_fn = llm_fn
        self._sent_suppression_keys: set[str] = set()

    async def compose_for_trigger(self, trigger_id: str) -> Optional[dict]:
        """
        Compose a message for a single trigger.

        Returns an action dict ready for the /v1/tick response, or None.
        """
        ctx = self.resolver.resolve(trigger_id)
        if not ctx:
            logger.warning(f"Could not resolve context for trigger {trigger_id}")
            return None

        # Dedup by suppression key
        sup_key = ctx.trigger.get("suppression_key", "")
        if sup_key and sup_key in self._sent_suppression_keys:
            logger.info(f"Suppressed duplicate: {sup_key}")
            return None

        # Build prompt
        system_prompt, user_prompt = build_prompt(ctx)

        # Call LLM
        try:
            raw = await self.llm_fn(system_prompt, user_prompt)
            composed = self._parse_llm_output(raw)
        except Exception as e:
            logger.error(f"LLM call failed for {trigger_id}: {e}")
            composed = self._fallback_compose(ctx)

        # Validate and fix
        is_valid, issues = validate(composed, ctx)
        if issues:
            logger.info(f"Validation issues for {trigger_id}: {issues}")

        # Mark as sent
        if sup_key:
            self._sent_suppression_keys.add(sup_key)

        # Build action response
        scope = ctx.trigger.get("scope", "merchant")
        return {
            "conversation_id": f"conv_{ctx.merchant_id}_{trigger_id}",
            "merchant_id": ctx.merchant_id,
            "customer_id": ctx.customer_id,
            "send_as": composed.get("send_as", "vera"),
            "trigger_id": trigger_id,
            "template_name": f"vera_{ctx.trigger.get('kind', 'generic')}_v1",
            "template_params": [
                ctx.merchant.get("identity", {}).get("name", ""),
                ctx.trigger.get("kind", ""),
                "",
            ],
            "body": composed.get("body", ""),
            "cta": composed.get("cta", "open_ended"),
            "suppression_key": composed.get("suppression_key", sup_key),
            "rationale": composed.get("rationale", ""),
        }

    def _parse_llm_output(self, raw: str) -> dict:
        """Extract JSON from LLM response."""
        # Try to find JSON in response
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # Fallback: treat entire response as body
        return {
            "body": raw.strip()[:800],
            "cta": "open_ended",
            "send_as": "vera",
            "suppression_key": "",
            "rationale": "Parsed from raw LLM output",
        }

    def _fallback_compose(self, ctx: ResolvedContext) -> dict:
        """Deterministic data-driven fallback when LLM is unavailable."""
        merchant = ctx.merchant
        trigger = ctx.trigger
        category = ctx.category
        
        m_name = merchant.get("identity", {}).get("owner_first_name", "there")
        biz_name = merchant.get("identity", {}).get("name", "your business")
        kind = trigger.get("kind", "update")
        scope = trigger.get("scope", "merchant")
        payload = trigger.get("payload", {})
        
        # Determine send_as
        send_as = "merchant_on_behalf" if scope == "customer" else "vera"
        
        # Build deterministic message based on kind
        body = ""
        cta = "open_ended"
        
        if kind == "research_digest":
            item_id = payload.get("top_item_id")
            item = next((i for i in category.get("digest", []) if i["id"] == item_id), {})
            title = item.get("title", "new research")
            source = item.get("source", "latest journals")
            body = f"Hi {m_name}, Vera here. Just saw a relevant item in {source}: '{title}'. It matches your patient profile. Want me to pull the abstract and draft a summary for you?"
            
        elif kind == "perf_dip":
            metric = payload.get("metric", "performance")
            delta = payload.get("delta_pct", "0")
            body = f"Hi {m_name}, noticed your {metric} is down by {delta}% this week. Based on peer data in {category.get('slug')}, this looks like a seasonal shift. Should we skip ad spend this week to save budget?"
            
        elif kind == "recall_due" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            slots = payload.get("available_slots", ["tomorrow"])
            slot_str = slots[0] if slots else "soon"
            body = f"Hi {c_name}, {biz_name} here! It's been a few months since your last visit. We have a slot available {slot_str}. Would you like to book a cleaning?"
            cta = "binary_yes_stop"
            
        elif kind == "ipl_match_today":
            match = payload.get("match", "the IPL match")
            body = f"Hi {m_name}, {match} is tonight. Usually, this leads to a dip in dine-in covers. Want me to push a BOGO delivery offer for tonight instead?"
            
        else:
            # Generic but clean
            if scope == "customer" and ctx.customer:
                c_name = ctx.customer.get("identity", {}).get("name", "there")
                body = f"Hi {c_name}, {biz_name} here. We have a personalized update for you. Would you like to hear more?"
                cta = "binary_yes_stop"
            else:
                body = f"Hi {m_name}, Vera here. I have a new insight for {biz_name} regarding {kind.replace('_', ' ')}. Interested in a quick breakdown?"

        return {
            "body": body,
            "cta": cta,
            "send_as": send_as,
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": f"Deterministic template-based composition for {kind}",
        }
