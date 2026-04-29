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

        # 3. Commitment / intent to proceed
        if self._is_commitment(msg_lower):
            return await self._handle_commitment(conv, merchant_id, message)

        # 4. General engagement — use LLM to compose reply
        return await self._compose_reply(conv, merchant_id, customer_id, message)

    def _is_auto_reply(self, msg_lower: str, conv: ConversationState) -> bool:
        # Pattern match
        for pattern in AUTO_REPLY_PATTERNS:
            if pattern in msg_lower:
                return True
        # Same verbatim message repeated
        if conv.last_auto_reply_text and msg_lower == conv.last_auto_reply_text:
            return True
        if len(conv.turns) >= 2:
            prev_msgs = [t["message"].lower().strip() for t in conv.turns if t["from"] != "vera"]
            if len(prev_msgs) >= 2 and prev_msgs[-1] == prev_msgs[-2]:
                conv.last_auto_reply_text = msg_lower
                return True
        return False

    def _is_hostile(self, msg_lower: str) -> bool:
        return any(p in msg_lower for p in HOSTILE_PATTERNS)

    def _is_commitment(self, msg_lower: str) -> bool:
        # Check if message is short and contains a commitment signal
        if len(msg_lower.split()) > 15:
            return False
        return any(p in msg_lower for p in COMMITMENT_PATTERNS)

    async def _handle_commitment(
        self, conv: ConversationState, merchant_id: str, message: str
    ) -> dict:
        """Switch to action mode — don't re-qualify."""
        merchant = self.store.get_merchant(merchant_id)
        name = "there"
        if merchant:
            name = merchant.get("identity", {}).get("owner_first_name", "there")

        return {
            "action": "send",
            "body": (
                f"Done, {name}! Setting this up now. "
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
        message: str,
    ) -> dict:
        """Use LLM to compose a contextual reply."""
        if not self.llm_fn:
            return {
                "action": "send",
                "body": "Got it — let me work on that. I'll have something ready for you shortly.",
                "cta": "open_ended",
                "rationale": "Acknowledged merchant message and advanced conversation",
            }

        merchant = self.store.get_merchant(merchant_id) or {}
        category_slug = merchant.get("category_slug", "")
        category = self.store.get_category(category_slug) or {}

        history = "\n".join(
            f"[{t['from'].upper()}] {t['message']}" for t in conv.turns[-6:]
        )

        system = (
            "You are Vera, magicpin's merchant AI assistant. Continue this conversation naturally. "
            "RULES: Don't re-introduce yourself. Don't ask qualifying questions if merchant already committed. "
            "Match language preference. Be concise (WhatsApp message). "
            "Respond with JSON only: {\"body\": \"...\", \"cta\": \"open_ended|none\", \"rationale\": \"...\"}"
        )
        user = (
            f"Category: {category.get('slug', '?')} (voice: {category.get('voice', {}).get('tone', '?')})\n"
            f"Merchant: {merchant.get('identity', {}).get('name', '?')} ({merchant.get('identity', {}).get('owner_first_name', '?')})\n"
            f"Languages: {merchant.get('identity', {}).get('languages', ['en'])}\n\n"
            f"CONVERSATION SO FAR:\n{history}\n\n"
            f"Compose Vera's next reply. JSON only."
        )

        try:
            import json
            raw = await self.llm_fn(system, user)
            match = re.search(r'\{[\s\S]*\}', raw)
            if match:
                data = json.loads(match.group())
                return {
                    "action": "send",
                    "body": data.get("body", "Got it, let me work on that."),
                    "cta": data.get("cta", "open_ended"),
                    "rationale": data.get("rationale", "LLM-composed reply"),
                }
        except Exception:
            pass

        return {
            "action": "send",
            "body": "Got it — working on this now. Will update you shortly.",
            "cta": "open_ended",
            "rationale": "Fallback reply after LLM error",
        }
