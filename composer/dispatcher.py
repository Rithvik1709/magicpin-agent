"""
Dispatcher: resolves context, calls LLM, validates output.

This is the main composition engine.
"""


from __future__ import annotations

import json
from datetime import datetime
import re
import logging
from typing import Optional, Callable, Awaitable

from .context_store import ContextStore
from .resolver import ContextResolver, ResolvedContext
from .prompts import build_prompt
from .validator import validate
from .utils import sanitize_for_logs

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
            logger.debug(
                "LLM composed: %s for %s — output sanitized: %s",
                ctx.trigger.get("kind"),
                ctx.merchant_id,
                sanitize_for_logs(raw)[:200],
            )
        except Exception as e:
            logger.warning(
                "LLM failed for %s: %s — falling back to deterministic",
                trigger_id,
                str(e),
            )
            composed = self._fallback_compose(ctx)

        # Step 4: Validate and fix
        is_valid, issues = validate(composed, ctx)
        if issues:
            issue_str = "; ".join(issues[:3])  # Log first 3 issues
            logger.debug("Validation issues for %s: %s", trigger_id, issue_str)

        # Step 5: Mark as sent
        if sup_key:
            self._sent_suppression_keys.add(sup_key)

        # Step 6: Build action response
        scope = ctx.trigger.get("scope", "merchant")
        action = {
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

        # Expose validation issues for transparency (may be empty)
        action["validation_issues"] = issues or []

        return action

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

    @staticmethod
    def _format_deadline(raw: str) -> str:
        """Convert ISO date string to a human-readable deadline."""
        if not raw:
            return "soon"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.strftime("%d %b %Y")  # e.g. "15 Dec 2026"
        except (ValueError, TypeError):
            return raw  # return as-is if already readable

    @staticmethod
    def _format_slot(slot) -> str:
        """Extract a human-readable label from a slot (dict or string)."""
        if isinstance(slot, dict):
            return slot.get("label") or slot.get("iso", "soon")
        return str(slot)

    @staticmethod
    def _format_delta(delta) -> str:
        """Format delta percentage for display."""
        try:
            val = float(delta)
            # Convert from decimal ratio (e.g., -0.50) to percentage
            if -1 < val < 1 and val != 0:
                return f"{abs(val * 100):.0f}"
            return f"{abs(val):.0f}"
        except (ValueError, TypeError):
            return str(delta)

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
            delta = self._format_delta(payload.get("delta_pct", 0))
            body = f"Hi {m_name}, noticed your {metric} is down by {delta}% this week. Based on peer data in {category.get('slug')}, this looks like a seasonal shift. Should we skip ad spend this week to save budget?"
            
        elif kind == "perf_spike":
            metric = payload.get("metric", "views")
            delta = self._format_delta(payload.get("delta_pct", 0))
            body = f"Hi {m_name}, Vera here! Your {metric} jumped {delta}% this week. This looks sustainable. Want to capitalize on this momentum with a targeted campaign?"
            cta = "binary_yes_stop"
            
        elif kind == "recall_due" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            slots = payload.get("available_slots", [])
            if slots:
                slot_str = self._format_slot(slots[0])
            else:
                slot_str = "soon"
            service = payload.get("service_due", "your check-up").replace("_", " ")
            body = f"Hi {c_name}, {m_full_name} here! It's been a few months since your last visit. We have a slot available on {slot_str}. Would you like to book?"
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"
            
        elif kind == "ipl_match_today":
            match_name = payload.get("match", "the IPL match")
            body = f"Hi {m_name}, {match_name} is tonight. Usually, this leads to a dip in dine-in covers. Want me to push a BOGO delivery offer for tonight instead?"
            
        elif kind == "renewal_due":
            days_left = payload.get("days_remaining", 0)
            plan = payload.get("plan") or merchant.get("subscription", {}).get("plan", "professional")
            amount = payload.get("renewal_amount")
            amount_str = f" (₹{amount})" if amount else ""
            body = f"Hi {m_name}, your {plan} subscription expires in {days_left} days{amount_str}. Want me to walk you through the renewal process? Takes just 2 minutes."
            cta = "binary_yes_stop"
            
        elif kind == "festival_upcoming":
            festival = payload.get("festival") or payload.get("festival_name", "the upcoming festival")
            days_until = payload.get("days_until", 30)
            date_str = payload.get("date", "")
            date_display = ""
            if date_str:
                date_display = f" ({self._format_deadline(date_str)})"
            body = f"Hi {m_name}, {festival}{date_display} is coming up in {days_until} days. This is a big footfall window for {category.get('slug')}. Want me to draft a seasonal offer for you?"
            cta = "binary_yes_stop"
            
        elif kind == "regulation_change":
            # Use deadline_iso (the actual field name in trigger data), fall back to deadline
            deadline_raw = payload.get("deadline_iso") or payload.get("deadline", "")
            deadline = self._format_deadline(deadline_raw)
            # Try to get the digest item for more detail
            item_id = payload.get("top_item_id")
            item = next((i for i in category.get("digest", []) if i.get("id") == item_id), {})
            change_title = item.get("title", payload.get("summary", "a regulation update"))
            body = f"Hi {m_name}, Vera here. There's a compliance update relevant to your business: {change_title}. Deadline: {deadline}. Want me to break it down for you and suggest next steps?"
            
        elif kind == "milestone_reached":
            metric = payload.get("metric", "reviews")
            value_now = payload.get("value_now")
            milestone_val = payload.get("milestone_value")
            is_imminent = payload.get("is_imminent", False)
            if is_imminent and value_now and milestone_val:
                gap = milestone_val - value_now
                body = f"Hi {m_name}, Vera here! You're just {gap} away from {milestone_val} {metric.replace('_', ' ')}! 🎉 Want to celebrate this milestone with a GBP post or customer thank-you campaign?"
            else:
                milestone = payload.get("milestone", f"{milestone_val} {metric.replace('_', ' ')}" if milestone_val else "a big milestone")
                body = f"Hi {m_name}, congrats! You've hit {milestone}! 🎉 Want to celebrate this with a GBP post or customer thank-you campaign?"
            cta = "binary_yes_stop"
            
        elif kind == "competitor_opened":
            competitor = payload.get("competitor_name", "a new competitor")
            distance = payload.get("distance_km", "nearby")
            their_offer = payload.get("their_offer", "")
            distance_str = f"{distance} km away" if isinstance(distance, (int, float)) else distance
            offer_str = f" They're offering {their_offer}." if their_offer else ""
            body = f"Hi {m_name}, {competitor} just opened {distance_str}.{offer_str} Let's sharpen your differentiation. What's one thing you offer that they don't?"
            
        elif kind == "review_theme_emerged":
            theme = payload.get("theme", "pricing").replace("_", " ")
            count = payload.get("occurrences_30d") or payload.get("count", 5)
            sentiment = payload.get("sentiment", "mixed")
            quote = payload.get("common_quote", "")
            quote_str = f" One customer said: '{quote}'." if quote else ""
            body = f"Hi {m_name}, I noticed customers are mentioning '{theme}' in reviews ({count} times, {sentiment} sentiment).{quote_str} Should we address this in a post or FAQ?"
            
        elif kind == "customer_lapsed_soft" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            body = f"Hi {c_name}, it's been a while! {m_full_name} would love to see you again. Got a slot next week if you're interested."
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"
            
        elif kind == "customer_lapsed_hard" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            prev_focus = payload.get("previous_focus", "").replace("_", " ")
            focus_str = f" We've got something new for your {prev_focus} goals." if prev_focus else ""
            body = f"Hi {c_name}, we miss you at {m_full_name}!{focus_str} Try us again this week — first visit is on us."
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"
            
        elif kind == "gbp_unverified":
            uplift = payload.get("estimated_uplift_pct", 0.23)
            # Convert from decimal to percentage if needed
            if isinstance(uplift, float) and uplift < 1:
                uplift = f"{uplift * 100:.0f}"
            body = f"Hi {m_name}, Vera here. Verified GBP gets ~{uplift}% more interactions. Let me start the verification for you (takes 5 mins). Want me to do that?"
            cta = "binary_yes_stop"
            
        elif kind == "supply_alert":
            molecule = payload.get("molecule", "a product")
            batches = payload.get("affected_batches", [])
            manufacturer = payload.get("manufacturer", "unknown")
            batch_str = ", ".join(batches[:3]) if batches else payload.get("batch_id", "unknown")
            body = f"Hi {m_name}, ALERT: {molecule} (batches {batch_str}, manufacturer {manufacturer}) has a recall notice. Let me draft a customer notification and replacement flow for you."

        elif kind == "chronic_refill_due" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            molecules = payload.get("molecule_list", [])
            mol_str = ", ".join(molecules) if molecules else "your medications"
            runout = self._format_deadline(payload.get("stock_runs_out_iso", ""))
            delivery = " We can deliver to your saved address." if payload.get("delivery_address_saved") else ""
            body = f"Hi {c_name}, {m_full_name} here. Your {mol_str} refill is due — stock runs out by {runout}.{delivery} Reply CONFIRM to dispatch."
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"

        elif kind == "trial_followup" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            trial_date = self._format_deadline(payload.get("trial_date", ""))
            sessions = payload.get("next_session_options", [])
            session_str = self._format_slot(sessions[0]) if sessions else "next week"
            body = f"Hi {c_name}, {m_full_name} here! Hope you enjoyed your trial on {trial_date}. We have a spot on {session_str} — would you like to continue?"
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"

        elif kind == "wedding_package_followup" and ctx.customer:
            c_name = ctx.customer.get("identity", {}).get("name", "there")
            days_to = payload.get("days_to_wedding", "")
            next_step = payload.get("next_step_window_open", "").replace("_", " ")
            body = f"Hi {c_name}, {m_full_name} here! With your wedding {days_to} days away, now's the perfect time to start your {next_step}. Want to book a session?"
            cta = "binary_yes_stop"
            send_as = "merchant_on_behalf"

        elif kind == "active_planning_intent":
            topic = payload.get("intent_topic", "your idea").replace("_", " ")
            body = f"Hi {m_name}, great! I've drafted a quick plan for the {topic}. Let me share it — you can tweak anything. Want me to send it over?"
            cta = "binary_yes_stop"

        elif kind == "dormant_with_vera":
            days_since = payload.get("days_since_last_merchant_message", "a while")
            last_topic = payload.get("last_topic", "").replace("_", " ")
            topic_ref = f" Last time we spoke about {last_topic}." if last_topic else ""
            body = f"Hi {m_name}, Vera here. It's been {days_since} days.{topic_ref} I've spotted a few new insights for {m_full_name}. Want a quick update?"

        elif kind == "winback_eligible":
            dip = self._format_delta(payload.get("perf_dip_pct", 0))
            lapsed = payload.get("lapsed_customers_added_since_expiry", 0)
            body = f"Hi {m_name}, Vera here. Since your subscription ended, your visibility dropped {dip}% and {lapsed} potential customers slipped by. Want to fix that with a quick reactivation?"
            cta = "binary_yes_stop"

        elif kind == "seasonal_perf_dip":
            metric = payload.get("metric", "views")
            delta = self._format_delta(payload.get("delta_pct", 0))
            season = payload.get("season_note", "this season").replace("_", " ")
            body = f"Hi {m_name}, your {metric} are down {delta}% — but this is typical for {season}. I'd suggest saving ad budget for the next high-conversion window. Focus on retaining your existing base for now."

        elif kind == "category_seasonal":
            trends = payload.get("trends", [])
            trend_str = ", ".join(t.replace("_", " ") for t in trends[:3]) if trends else "seasonal shifts"
            body = f"Hi {m_name}, Vera here. Summer demand data is in: {trend_str}. Want me to suggest shelf adjustments based on this?"

        elif kind == "cde_opportunity":
            item_id = payload.get("digest_item_id")
            item = next((i for i in category.get("digest", []) if i.get("id") == item_id), {})
            title = item.get("title", "an upcoming CDE event")
            credits = payload.get("credits", "")
            fee = payload.get("fee", "")
            credit_str = f" ({credits} credits, {fee})" if credits else ""
            body = f"Hi {m_name}, Vera here. Found a CDE opportunity: {title}{credit_str}. Interested?"

        elif kind == "curious_ask_due":
            template = payload.get("ask_template", "business_update").replace("_", " ")
            body = f"Hi {m_name}, Vera here — quick question: {template}? I can turn your answer into a GBP post or WhatsApp update for your customers."
            
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
