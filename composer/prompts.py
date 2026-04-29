"""
Prompt templates for the LLM composer.

Each function builds (system_prompt, user_prompt) from a ResolvedContext.
The system prompt enforces voice rules + constraints.
The user prompt provides the context-specific data for this trigger kind.
"""

from __future__ import annotations
import json
from .resolver import ResolvedContext


def _voice_block(category: dict) -> str:
    voice = category.get("voice", {})
    tone = voice.get("tone", "professional")
    taboos = voice.get("vocab_taboo", [])
    allowed = voice.get("vocab_allowed", [])
    return (
        f"Voice tone: {tone}\n"
        f"Allowed vocabulary: {', '.join(allowed[:10])}\n"
        f"TABOO words (NEVER use): {', '.join(taboos)}"
    )


def _merchant_block(merchant: dict) -> str:
    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    offers = [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]
    signals = merchant.get("signals", [])
    conv = merchant.get("conversation_history", [])
    last_conv = conv[-1] if conv else None
    agg = merchant.get("customer_aggregate", {})

    lines = [
        f"Name: {identity.get('name', 'Unknown')}",
        f"Owner: {identity.get('owner_first_name', 'Unknown')}",
        f"City: {identity.get('city', '?')}, Locality: {identity.get('locality', '?')}",
        f"Languages: {identity.get('languages', ['en'])}",
        f"Subscription: {merchant.get('subscription', {}).get('status', '?')} ({merchant.get('subscription', {}).get('plan', '?')}), {merchant.get('subscription', {}).get('days_remaining', '?')} days left",
        f"Performance (30d): views={perf.get('views', '?')}, calls={perf.get('calls', '?')}, CTR={perf.get('ctr', '?')}, directions={perf.get('directions', '?')}",
        f"7d delta: {json.dumps(perf.get('delta_7d', {}))}",
        f"Active offers: {offers if offers else 'none'}",
        f"Signals: {signals}",
        f"Customer aggregate: {json.dumps(agg)}",
    ]
    if last_conv:
        lines.append(f"Last Vera touch: {last_conv.get('ts', '?')} — {last_conv.get('body', '')[:100]}")
        lines.append(f"Engagement: {last_conv.get('engagement', '?')}")
    if merchant.get("review_themes"):
        themes = [f"{t['theme']}({t['sentiment']}, {t.get('occurrences_30d', '?')}x)" for t in merchant["review_themes"][:3]]
        lines.append(f"Review themes: {', '.join(themes)}")
    return "\n".join(lines)


def _customer_block(customer: dict) -> str:
    if not customer:
        return "No customer (merchant-facing message)"
    identity = customer.get("identity", {})
    rel = customer.get("relationship", {})
    prefs = customer.get("preferences", {})
    return (
        f"Customer name: {identity.get('name', '?')}\n"
        f"Language pref: {identity.get('language_pref', 'en')}\n"
        f"State: {customer.get('state', '?')}\n"
        f"Visits: {rel.get('visits_total', '?')}, Last: {rel.get('last_visit', '?')}\n"
        f"Services: {rel.get('services_received', [])}\n"
        f"Preferred slots: {prefs.get('preferred_slots', '?')}\n"
        f"Consent scope: {customer.get('consent', {}).get('scope', [])}"
    )


def _trigger_block(trigger: dict) -> str:
    return (
        f"Trigger kind: {trigger.get('kind', '?')}\n"
        f"Scope: {trigger.get('scope', '?')}\n"
        f"Source: {trigger.get('source', '?')}\n"
        f"Urgency: {trigger.get('urgency', '?')}/5\n"
        f"Payload: {json.dumps(trigger.get('payload', {}))}\n"
        f"Suppression key: {trigger.get('suppression_key', '')}"
    )


def _digest_item_for_trigger(category: dict, trigger: dict) -> str:
    """Find the digest item referenced by the trigger payload."""
    top_item_id = trigger.get("payload", {}).get("top_item_id")
    if not top_item_id:
        return ""
    for item in category.get("digest", []):
        if item.get("id") == top_item_id:
            return f"Digest item: {json.dumps(item)}"
    return ""


SYSTEM_PROMPT = """You are Vera, magicpin's AI merchant assistant. You compose WhatsApp messages for merchants and their customers.

RULES (STRICT):
1. NEVER fabricate data not present in the context below. No fake numbers, no fake citations.
2. Use the merchant's name/owner name. Match their language preference (Hindi-English mix if languages include "hi").
3. Single primary CTA per message. Binary YES/STOP for actions; open-ended for questions; none for pure info.
4. NO preambles ("I hope you're doing well"). Get to the point immediately.
5. NO promotional hype ("AMAZING DEAL!"). Use peer/colleague tone matching the category voice.
6. Anchor on verifiable facts: numbers, dates, source citations from the context.
7. Keep concise — WhatsApp messages, not emails.
8. For customer-facing (scope=customer): send_as MUST be "merchant_on_behalf". For merchant-facing: send_as MUST be "vera".

OUTPUT FORMAT — respond with ONLY this JSON, no markdown fences:
{"body": "the message text", "cta": "binary_yes_stop|open_ended|none", "send_as": "vera|merchant_on_behalf", "suppression_key": "from trigger", "rationale": "1-2 sentence explanation of why this message, what lever it uses"}
"""


def build_prompt(ctx: ResolvedContext) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the LLM."""
    category = ctx.category
    kind = ctx.trigger.get("kind", "generic")

    # Build kind-specific instructions
    kind_instructions = KIND_INSTRUCTIONS.get(kind, KIND_INSTRUCTIONS["_default"])

    digest_info = _digest_item_for_trigger(category, ctx.trigger)
    peer_stats = category.get("peer_stats", {})

    user_prompt = f"""COMPOSE A MESSAGE for this context:

=== CATEGORY ({category.get('slug', '?')}) ===
{_voice_block(category)}
Peer stats: avg_rating={peer_stats.get('avg_rating', '?')}, avg_ctr={peer_stats.get('avg_ctr', '?')}, avg_reviews={peer_stats.get('avg_review_count', '?')}
{digest_info}

=== MERCHANT ===
{_merchant_block(ctx.merchant)}

=== TRIGGER ===
{_trigger_block(ctx.trigger)}

=== CUSTOMER ===
{_customer_block(ctx.customer)}

=== COMPOSITION GUIDANCE FOR "{kind}" ===
{kind_instructions}

Compose the message now. Remember: JSON only, no markdown."""

    return SYSTEM_PROMPT, user_prompt


# ─── Per-kind composition guidance ──────────────────────────────

KIND_INSTRUCTIONS = {
    "research_digest": (
        "Frame around the specific research finding. Cite the source (journal, page). "
        "Connect to THIS merchant's patient cohort if possible. "
        "Offer to pull the abstract + draft a patient-ed message. Curiosity + reciprocity levers."
    ),
    "regulation_change": (
        "Lead with urgency — deadline date. Explain what changed and what action is needed. "
        "Offer to help with the compliance step. Loss aversion lever."
    ),
    "recall_due": (
        "Customer-facing (send_as=merchant_on_behalf). Use merchant's name, not Vera. "
        "Reference last visit date, recall window. Offer specific slots matching customer's preference. "
        "Include the service price from merchant's active offers. Hindi-English mix if customer prefers."
    ),
    "perf_dip": (
        "Anchor on the specific metric and delta. Don't alarm — diagnose. "
        "If seasonal, say so. Suggest one concrete action. Loss aversion + effort externalization."
    ),
    "perf_spike": (
        "Celebrate the uptick with the specific number. Attribute likely cause. "
        "Suggest how to sustain it. Social proof + momentum lever."
    ),
    "milestone_reached": (
        "Congratulate with the specific number. If imminent (not yet crossed), frame as 'X away from Y'. "
        "Suggest a celebratory action (GBP post, customer thank-you). Reciprocity lever."
    ),
    "renewal_due": (
        "Lead with value delivered during current subscription (use perf numbers). "
        "State days remaining + renewal amount. Single binary CTA. Loss aversion."
    ),
    "festival_upcoming": (
        "Connect the festival to category-specific opportunity. "
        "Suggest a concrete offer or post. If days_until > 30, frame as 'planning window'. "
        "Effort externalization (I'll draft it)."
    ),
    "ipl_match_today": (
        "Operator-to-operator advice. Consider if weeknight vs weekend affects footfall. "
        "Reference existing offers. Suggest delivery-pivot if covers likely to drop. "
        "Counter-intuitive data is powerful. Effort externalization."
    ),
    "review_theme_emerged": (
        "Surface the pattern: theme, count, representative quote. "
        "If negative: suggest response strategy. If positive: suggest amplification. "
        "Social proof + specificity."
    ),
    "competitor_opened": (
        "Voyeur curiosity — name, distance, their offer. "
        "Help merchant differentiate (what they have that competitor doesn't). "
        "Don't be alarmist. Curiosity + competitive framing."
    ),
    "supply_alert": (
        "URGENT tone. Lead with batch numbers + manufacturer. "
        "Quantify affected customers from merchant's data. "
        "Offer workflow: draft customer notifications + replacement process. Urgency + specificity."
    ),
    "chronic_refill_due": (
        "Customer-facing (send_as=merchant_on_behalf). Respectful tone for seniors. "
        "List all molecules. State run-out date. Include delivery option + pricing if available. "
        "Single binary CTA (CONFIRM to dispatch)."
    ),
    "customer_lapsed_hard": (
        "Customer-facing (send_as=merchant_on_behalf). NO shame, NO guilt. "
        "Reference their previous focus/goal. Suggest something new that matches. "
        "Free trial or no-commitment offer. Binary CTA."
    ),
    "customer_lapsed_soft": (
        "Customer-facing (send_as=merchant_on_behalf). Gentle reminder. "
        "Reference relationship. Offer specific slot. Binary CTA."
    ),
    "curious_ask_due": (
        "Ask the merchant a question about their business this week. "
        "Offer to turn their answer into content (GBP post, WhatsApp reply). "
        "Low-stakes, no commitment. Asking-the-merchant lever."
    ),
    "winback_eligible": (
        "Merchant's subscription expired. Lead with what they're missing (perf dip since expiry). "
        "Quantify: lapsed customers added since. Offer a quick win to re-engage. "
        "Loss aversion + effort externalization."
    ),
    "active_planning_intent": (
        "Merchant expressed intent — DO NOT re-qualify. Switch to action mode. "
        "Draft a concrete artifact (pricing tiers, post copy, package details). "
        "Make it editable. Offer the next step. Effort externalization."
    ),
    "dormant_with_vera": (
        "Re-engage with a value hook, not a guilt trip. "
        "Reference what's changed since last conversation. "
        "Lead with a new insight or data point. Curiosity lever."
    ),
    "gbp_unverified": (
        "Explain the uplift from verification (use the estimated_uplift_pct). "
        "Walk them through the process (postcard/phone). "
        "Effort externalization (I'll start it for you). Specificity."
    ),
    "cde_opportunity": (
        "Invite to the specific event. Cite credits, fee, date. "
        "Link to their professional development. Low-friction CTA."
    ),
    "category_seasonal": (
        "Surface the seasonal demand shifts with specific numbers. "
        "Suggest shelf/menu/schedule adjustments. Effort externalization."
    ),
    "seasonal_perf_dip": (
        "Normalize the dip — this is expected for the season. Cite the range. "
        "Reframe: skip ad spend now, save for high-conversion window. "
        "Focus on retention of existing base. Anxiety pre-emption."
    ),
    "wedding_package_followup": (
        "Customer-facing (send_as=merchant_on_behalf). Reference wedding date, days until. "
        "Suggest the next step in the bridal journey (skin prep, trial, etc). "
        "Use merchant's owner name. Urgency + specificity."
    ),
    "trial_followup": (
        "Customer-facing (send_as=merchant_on_behalf). Reference the trial date. "
        "Offer specific next session. Low-commitment CTA."
    ),
    "_default": (
        "Compose a contextually appropriate message using all available context. "
        "Anchor on the most compelling data point. Use one compulsion lever. Single CTA."
    ),
}
