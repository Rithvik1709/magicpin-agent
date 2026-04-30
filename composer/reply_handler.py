"""
Multi-turn reply handler.

Handles /v1/reply — detects auto-replies, classifies intent,
manages conversation state.
"""

from __future__ import annotations

import re
from typing import Optional
from .context_store import ContextStore


# Known auto-reply patterns (WhatsApp Business canned responses)
AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "our team will respond",
    "we will get back to you",
    "automated assistant",
    "automated reply",
    "your message has been received",
    "thanks for reaching out",
    "we'll respond shortly",
    "thank you for your message",
]

# Hostile / not-interested signals
HOSTILE_PATTERNS = [
    "stop messaging", "stop texting", "leave me alone", "don't contact",
    "not interested", "unsubscribe", "spam", "useless", "waste of time",
    "band karo", "mat bhejo", "pareshan mat karo",
]

# Explicit conversation-end signals (binary CTA responses)
STOP_PATTERNS = [
    "stop", "no", "nahi", "nope", "no thanks", "no thank you",
    "not now", "later", "baad mein", "abhi nahi",
]

# Commitment / intent signals
COMMITMENT_PATTERNS = [
    "yes", "ok let's do it", "go ahead", "let's do it",
    "haan", "chalega", "kar do", "proceed", "confirm",
    "sure", "sounds good", "do it", "let's go",
    "ok done", "theek hai", "chalo",
]


class ConversationState:
    """Track state for a single conversation."""

    def __init__(self, conversation_id: str, merchant_id: str):
        self.conversation_id = conversation_id
        self.merchant_id = merchant_id
        self.turns: list[dict] = []
        self.auto_reply_count = 0
        self.last_auto_reply_text: Optional[str] = None

    def add_turn(self, from_role: str, message: str):
        self.turns.append({"from": from_role, "message": message})


class ReplyHandler:
    """Handle incoming merchant/customer replies."""

    def __init__(self, store: ContextStore, llm_fn=None):
        self.store = store
        self.conversations: dict[str, ConversationState] = {}
        self.llm_fn = llm_fn  # async function(system, user) -> str

    def get_or_create_conversation(
        self, conversation_id: str, merchant_id: str
    ) -> ConversationState:
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = ConversationState(
                conversation_id, merchant_id
            )
        return self.conversations[conversation_id]

    async def handle_reply(
        self,
        conversation_id: str,
        merchant_id: str,
        customer_id: Optional[str],
        from_role: str,
        message: str,
        turn_number: int,
    ) -> dict:
        """
        Process a reply and return the bot's response.

        Returns dict with keys: action, body (optional), cta (optional),
        rationale, wait_seconds (optional).
        """
        conv = self.get_or_create_conversation(conversation_id, merchant_id)
        conv.add_turn(from_role, message)
        msg_lower = message.lower().strip()

        # 1. Auto-reply detection
        if self._is_auto_reply(msg_lower, conv):
            conv.auto_reply_count += 1
            if conv.auto_reply_count >= 2:
                return {
                    "action": "end",
                    "rationale": f"Auto-reply detected {conv.auto_reply_count} times — exiting gracefully",
                }
            return {
                "action": "send",
                "body": "Samajh gayi — agar aap khud baat karna chahein toh reply kar dijiye. Main yahan hoon. 🙂",
                "cta": "none",
                "rationale": "First auto-reply detected; giving one more chance for human response",
            }

        # 2. Hostile / not-interested
        if self._is_hostile(msg_lower):
            return {
                "action": "end",
                "rationale": "Merchant signaled not interested or hostile — gracefully exiting",
            }

        # 3. Explicit STOP (binary CTA response = end conversation)
        if self._is_stop(msg_lower):
            return {
                "action": "end",
                "rationale": "User sent STOP or declined — ending conversation gracefully",
            }

        # 4. Commitment / intent to proceed
        if self._is_commitment(msg_lower):
            return await self._handle_commitment(conv, merchant_id, customer_id, from_role, message)

        # 5. General engagement — use LLM to compose reply
        return await self._compose_reply(conv, merchant_id, customer_id, from_role, message)

    def _is_auto_reply(self, msg_lower: str, conv: ConversationState) -> bool:
        # Pattern match against known auto-reply templates
        for pattern in AUTO_REPLY_PATTERNS:
            if pattern in msg_lower:
                conv.last_auto_reply_text = msg_lower
                return True
        
        # Check if same message repeated verbatim (indicates bot loop)
        if conv.last_auto_reply_text and msg_lower == conv.last_auto_reply_text:
            return True
        
        # Check if last two merchant messages are identical (strong auto-reply signal)
        if len(conv.turns) >= 2:
            prev_msgs = [t["message"].lower().strip() for t in conv.turns if t["from"] != "vera"]
            if len(prev_msgs) >= 2 and prev_msgs[-1] == prev_msgs[-2]:
                conv.last_auto_reply_text = msg_lower
                return True
        
        return False

    def _is_hostile(self, msg_lower: str) -> bool:
        return any(p in msg_lower for p in HOSTILE_PATTERNS)

    def _is_stop(self, msg_lower: str) -> bool:
        """Detect explicit STOP / decline signals (binary CTA response)."""
        # Exact match for short signals
        stripped = msg_lower.strip().rstrip(".!")
        if stripped in STOP_PATTERNS:
            return True
        # Also catch standalone STOP anywhere in short messages
        if len(msg_lower.split()) <= 3 and "stop" in msg_lower:
            return True
        return False

    def _is_commitment(self, msg_lower: str) -> bool:
        # Check if message is short and contains a commitment signal
        if len(msg_lower.split()) > 15:
            return False
        return any(p in msg_lower for p in COMMITMENT_PATTERNS)

    async def _handle_commitment(
        self, conv: ConversationState, merchant_id: str, customer_id: str | None,
        from_role: str, message: str
    ) -> dict:
        """Switch to action mode — don't re-qualify.
        
        Addresses the correct person: if the reply is from a customer, address
        the customer by name (not the merchant owner).
        """
        merchant = self.store.get_merchant(merchant_id)
        m_name = "there"
        if merchant:
            m_name = merchant.get("identity", {}).get("owner_first_name", "there")

        # Determine who we're talking to
        if from_role == "customer" and customer_id:
            customer = self.store.get_customer(customer_id)
            name = customer.get("identity", {}).get("name", "there") if customer else "there"
            m_full = merchant.get("identity", {}).get("name", "us") if merchant else "us"
            return {
                "action": "send",
                "body": (
                    f"Done, {name}! We're setting this up now. "
                    f"You'll get a confirmation from {m_full} shortly — should take about 5 minutes. "
                    "Anything else you need?"
                ),
                "cta": "open_ended",
                "rationale": "Customer committed — switched to action mode, addressing customer by name",
            }

        return {
            "action": "send",
            "body": (
                f"Done, {m_name}! Setting this up now. "
                "I'll send you a confirmation once it's live — should take about 5 minutes. "
                "Anything else you'd like me to adjust?"
            ),
            "cta": "open_ended",
            "rationale": "Merchant committed — switched to action mode immediately, no re-qualification",
        }

    async def _compose_reply(
        self,
        conv: ConversationState,
        merchant_id: str,
        customer_id: Optional[str],
        from_role: str,
        message: str,
    ) -> dict:
        """Use LLM to compose a contextual reply."""
        merchant = self.store.get_merchant(merchant_id) or {}
        m_name = merchant.get("identity", {}).get("owner_first_name", "there")
        m_full = merchant.get("identity", {}).get("name", "your business")

        # Resolve customer if applicable
        customer = self.store.get_customer(customer_id) if customer_id else None
        c_name = customer.get("identity", {}).get("name", "there") if customer else None

        # Determine who we're talking to for the fallback
        is_customer_conv = from_role == "customer" and c_name
        addressee = c_name if is_customer_conv else m_name

        if not self.llm_fn:
            if is_customer_conv:
                return {
                    "action": "send",
                    "body": f"Got it, {c_name}! Let me set that up for you. {m_full} will confirm shortly.",
                    "cta": "open_ended",
                    "rationale": "Acknowledged customer message — no LLM, using deterministic fallback",
                }
            return {
                "action": "send",
                "body": f"Got it, {m_name} — let me work on that. I'll have something ready for you shortly.",
                "cta": "open_ended",
                "rationale": "Acknowledged merchant message — no LLM, using deterministic fallback",
            }

        category_slug = merchant.get("category_slug", "")
        category = self.store.get_category(category_slug) or {}

        history = "\n".join(
            f"[{t['from'].upper()}] {t['message']}" for t in conv.turns[-6:]
        )

        customer_context = ""
        if customer:
            customer_context = (
                f"Customer name: {c_name}\n"
                f"Customer language pref: {customer.get('identity', {}).get('language_pref', 'en')}\n"
            )

        system = (
            "You are Vera, magicpin's merchant AI assistant. Continue this conversation naturally. "
            "RULES: Don't re-introduce yourself. Don't ask qualifying questions if merchant/customer already committed. "
            "CRITICAL: Address the CORRECT person. If the last message is from a customer, address the customer by their name, NOT the merchant. "
            "If talking to a customer, speak on behalf of the merchant business. "
            "Match language preference. Be concise (WhatsApp message). "
            "If the user says STOP, no, not interested — respond with action=end. "
            "Respond with JSON only: {\"body\": \"...\", \"cta\": \"open_ended|none\", \"action\": \"send|end\", \"rationale\": \"...\"}"
        )
        user = (
            f"Category: {category.get('slug', '?')} (voice: {category.get('voice', {}).get('tone', '?')})\n"
            f"Merchant: {merchant.get('identity', {}).get('name', '?')} (owner: {merchant.get('identity', {}).get('owner_first_name', '?')})\n"
            f"Languages: {merchant.get('identity', {}).get('languages', ['en'])}\n"
            f"{customer_context}"
            f"From role: {from_role}\n\n"
            f"CONVERSATION SO FAR:\n{history}\n\n"
            f"Compose the next reply addressed to the {from_role}. JSON only."
        )

        try:
            import json
            raw = await self.llm_fn(system, user)
            match = re.search(r'\{[\s\S]*\}', raw)
            if match:
                data = json.loads(match.group())
                action = data.get("action", "send")
                if action == "end":
                    return {
                        "action": "end",
                        "rationale": data.get("rationale", "LLM determined conversation should end"),
                    }
                return {
                    "action": "send",
                    "body": data.get("body", f"Got it, {addressee}. Let me work on that."),
                    "cta": data.get("cta", "open_ended"),
                    "rationale": data.get("rationale", "LLM-composed reply"),
                }
        except Exception:
            pass

        # Deterministic fallback when LLM fails
        if is_customer_conv:
            return {
                "action": "send",
                "body": f"Got it, {c_name}! Working on this now. {m_full} will update you shortly.",
                "cta": "open_ended",
                "rationale": "Fallback reply to customer after LLM error",
            }
        return {
            "action": "send",
            "body": f"Got it, {m_name} — working on this now. Will update you shortly.",
            "cta": "open_ended",
            "rationale": "Fallback reply to merchant after LLM error",
        }
