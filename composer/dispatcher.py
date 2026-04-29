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

        Returns an action dict ready for the /v1/tick response, or None if composition fails.
        """
        # Step 1: Resolve context
        ctx = self.resolver.resolve(trigger_id)
        if not ctx:
            logger.warning(f"Failed to resolve context for trigger {trigger_id}")
            return None

        # Step 2: Check suppression
        sup_key = ctx.trigger.get("suppression_key", "")
        if sup_key and sup_key in self._sent_suppression_keys:
            logger.debug(f"Suppressed duplicate: {sup_key}")
            return None

        # Step 3: Build prompt and call LLM or fallback
        system_prompt, user_prompt = build_prompt(ctx)
        composed = None

        try:
            raw = await self.llm_fn(system_prompt, user_prompt)
            composed = self._parse_llm_output(raw)
            logger.debug(f"LLM composed: {ctx.trigger.get('kind')} for {ctx.merchant_id}")
        except Exception as e:
            logger.warning(f"LLM failed for {trigger_id}: {e} — falling back to deterministic")
            composed = self._fallback_compose(ctx)

        # Step 4: Validate and fix
        is_valid, issues = validate(composed, ctx)
        if issues:
            issue_str = "; ".join(issues[:3])  # Log first 3 issues
            logger.debug(f"Validation issues for {trigger_id}: {issue_str}")

        # Step 5: Mark as sent
        if sup_key:
            self._sent_suppression_keys.add(sup_key)

        # Step 6: Build action response
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
        m_full_name = merchant.get("identity", {}).get("name", "your business")
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
            
        elif kind == "perf_spike":
            metric = payload.get("metric", "views")
            delta = payload.get("delta_pct", "0")
            body = f"Hi {m_name}, Vera here! Your {metric} jumped {delta}% this week. This looks sustainable. Want to capitalize on this momentum with a targeted campaign?"
            cta = "binary_yes_stop"
            
        elif kind == "recall_due" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            slots = payload.get("available_slots", ["tomorrow"])
            slot_str = slots[0] if slots else "soon"
            body = f"Hi {c_name}, {m_full_name} here! It's been a few months since your last visit. We have a slot available {slot_str}. Would you like to book?"
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"
            
        elif kind == "ipl_match_today":
            match = payload.get("match", "the IPL match")
            body = f"Hi {m_name}, {match} is tonight. Usually, this leads to a dip in dine-in covers. Want me to push a BOGO delivery offer for tonight instead?"
            
        elif kind == "renewal_due":
            days_left = payload.get("days_remaining", "0")
            plan = merchant.get("subscription", {}).get("plan", "professional")
            body = f"Hi {m_name}, your {plan} subscription expires in {days_left} days. Want me to walk you through the renewal process? Takes just 2 minutes."
            cta = "binary_yes_stop"
            
        elif kind == "festival_upcoming":
            festival = payload.get("festival_name", "the upcoming festival")
            days_until = payload.get("days_until", "30")
            body = f"Hi {m_name}, {festival} is coming up in {days_until} days. This is a big footfall window for {category.get('slug')}. Want me to draft a seasonal offer for you?"
            cta = "binary_yes_stop"
            
        elif kind == "regulation_change":
            change = payload.get("summary", "a regulation update")
            deadline = payload.get("deadline", "soon")
            body = f"Hi {m_name}, Vera here. There's a compliance update relevant to your business. Deadline: {deadline}. Want me to break it down for you and suggest next steps?"
            
        elif kind == "milestone_reached":
            milestone = payload.get("milestone", "100 reviews")
            body = f"Hi {m_name}, congrats! You've hit {milestone}! 🎉 Want to celebrate this with a GBP post or customer thank-you campaign?"
            cta = "binary_yes_stop"
            
        elif kind == "competitor_opened":
            competitor = payload.get("competitor_name", "a new competitor")
            distance = payload.get("distance_km", "nearby")
            body = f"Hi {m_name}, {competitor} just opened {distance}. Let's sharpen your differentiation. What's one thing you offer that they don't?"
            
        elif kind == "review_theme_emerged":
            theme = payload.get("theme", "pricing")
            count = payload.get("count", "5")
            sentiment = payload.get("sentiment", "mixed")
            body = f"Hi {m_name}, I noticed customers are mentioning '{theme}' in reviews ({count} times, {sentiment}). Should we address this in a post or FAQ?"
            
        elif kind == "customer_lapsed_soft" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            body = f"Hi {c_name}, it's been a while! We'd love to see you again. Got a slot next week if you're interested."
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"
            
        elif kind == "customer_lapsed_hard" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            body = f"Hi {c_name}, we miss you! Try us again this week — first visit is on us."
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"
            
        elif kind == "gbp_unverified":
            uplift = payload.get("estimated_uplift_pct", "23")
            body = f"Hi {m_name}, Vera here. Verified GBP gets ~{uplift}% more interactions. Let me start the verification for you (takes 5 mins). Want me to do that?"
            cta = "binary_yes_stop"
            
        elif kind == "supply_alert":
            product = payload.get("product", "a product")
            batch = payload.get("batch_id", "unknown")
            body = f"Hi {m_name}, ALERT: {product} (batch {batch}) may have an issue. Let me draft a customer notification and replacement flow for you."
            
        else:
            # Generic fallback with context awareness
            if scope == "customer" and ctx.customer:
                c_name = ctx.customer.get("identity", {}).get("name", "there")
                body = f"Hi {c_name}, {m_full_name} here. We have a personalized update for you. Want to hear more?"
                cta = "binary_yes_stop"
            else:
                body = f"Hi {m_name}, Vera here. I have a new insight for {m_full_name} regarding {kind.replace('_', ' ')}. Interested in a quick breakdown?"

        return {
            "body": body,
            "cta": cta,
            "send_as": send_as,
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": f"Deterministic template-based composition for {kind}",
        }
