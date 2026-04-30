"""
Vera Bot — magicpin AI Challenge

FastAPI server implementing the 5 required endpoints:
  GET  /v1/healthz   — liveness probe
  GET  /v1/metadata  — bot identity
  POST /v1/context   — receive context pushes
  POST /v1/tick      — periodic wake-up; bot initiates conversations
  POST /v1/reply     — receive merchant/customer replies

Configure via environment variables:
  LLM_PROVIDER  — "openai" (default), "anthropic", "gemini", "deepseek"
  LLM_API_KEY   — your API key
  LLM_MODEL     — model name (default: provider-specific)
  PORT          — server port (default: 8080)
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from composer.context_store import ContextStore
from composer.dispatcher import Dispatcher
from composer.reply_handler import ReplyHandler

# ─── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vera-bot")

# ─── App + State ──────────────────────────────────────────────────

app = FastAPI(title="Vera Bot", version="1.0.0")
START_TIME = time.time()

store = ContextStore()

# ─── LLM Client ──────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# Track LLM health — skip calls after repeated failures to avoid 25s timeouts
_llm_consecutive_failures = 0
_LLM_FAILURE_THRESHOLD = 3  # After this many consecutive failures, skip LLM
_llm_last_success_time = 0.0


def _make_llm_request(url: str, body: bytes, headers: dict, timeout: int = 15) -> dict:
    """Make a single HTTP request to an LLM API with proper error handling."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode("utf-8"))


def _build_openai_compatible_request(
    url: str, model: str, system: str, user: str, api_key: str
) -> tuple[str, bytes, dict]:
    """Build request for OpenAI-compatible APIs (OpenAI, DeepSeek, Groq)."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 1200,
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return url, body, headers


async def llm_complete(system: str, user: str) -> str:
    """Call the configured LLM provider with retry and health tracking.

    Raises Exception if all attempts fail (caller should catch and use fallback).
    """
    global _llm_consecutive_failures, _llm_last_success_time
    import urllib.error

    # Fast-fail: no API key configured
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY not configured — using deterministic fallback")

    # Fast-fail: LLM has been failing repeatedly, skip for a cooldown period
    if _llm_consecutive_failures >= _LLM_FAILURE_THRESHOLD:
        # Allow retry every 60 seconds
        if time.time() - _llm_last_success_time < 60:
            raise RuntimeError(
                f"LLM circuit breaker open ({_llm_consecutive_failures} consecutive failures) "
                f"— using deterministic fallback"
            )
        else:
            logger.info("LLM circuit breaker: attempting recovery after cooldown")

    # Build provider-specific request
    if LLM_PROVIDER == "openai":
        model = LLM_MODEL or "gpt-4o-mini"
        url, body, headers = _build_openai_compatible_request(
            "https://api.openai.com/v1/chat/completions", model, system, user, LLM_API_KEY
        )
        extract = lambda data: data["choices"][0]["message"]["content"]

    elif LLM_PROVIDER == "anthropic":
        model = LLM_MODEL or "claude-3-5-sonnet-20241022"
        body = json.dumps({
            "model": model,
            "max_tokens": 1200,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": LLM_API_KEY,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        extract = lambda data: data["content"][0]["text"]

    elif LLM_PROVIDER == "gemini":
        model = LLM_MODEL or "gemini-1.5-flash"
        full_prompt = f"{system}\n\n{user}"
        body = json.dumps({
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1200},
        }).encode("utf-8")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={LLM_API_KEY}"
        headers = {"Content-Type": "application/json"}
        extract = lambda data: data["candidates"][0]["content"]["parts"][0]["text"]

    elif LLM_PROVIDER == "deepseek":
        model = LLM_MODEL or "deepseek-chat"
        url, body, headers = _build_openai_compatible_request(
            "https://api.deepseek.com/v1/chat/completions", model, system, user, LLM_API_KEY
        )
        extract = lambda data: data["choices"][0]["message"]["content"]

    elif LLM_PROVIDER == "groq":
        model = LLM_MODEL or "llama-3.1-70b-versatile"
        url, body, headers = _build_openai_compatible_request(
            "https://api.groq.com/openai/v1/chat/completions", model, system, user, LLM_API_KEY
        )
        extract = lambda data: data["choices"][0]["message"]["content"]

    else:
        raise ValueError(f"Unknown LLM provider: {LLM_PROVIDER}")

    # Retry with backoff (2 attempts: 0s, then 1s delay)
    last_error = None
    for attempt in range(2):
        try:
            if attempt > 0:
                import asyncio
                await asyncio.sleep(1.0)  # Brief backoff before retry
                logger.info(f"LLM retry attempt {attempt + 1}")

            data = _make_llm_request(url, body, headers, timeout=15)
            result = extract(data)

            # Success — reset failure counter
            _llm_consecutive_failures = 0
            _llm_last_success_time = time.time()
            return result

        except urllib.error.HTTPError as e:
            last_error = e
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass

            if e.code == 401:
                # Auth failure — no point retrying
                _llm_consecutive_failures += 1
                raise RuntimeError(f"LLM auth failed (401): check LLM_API_KEY. {error_body}")
            elif e.code == 429:
                # Rate limited — retry with longer backoff
                logger.warning(f"LLM rate limited (429), attempt {attempt + 1}")
                if attempt == 0:
                    import asyncio
                    await asyncio.sleep(2.0)
                continue
            elif e.code >= 500:
                # Server error — retry
                logger.warning(f"LLM server error ({e.code}), attempt {attempt + 1}")
                continue
            else:
                _llm_consecutive_failures += 1
                raise RuntimeError(f"LLM HTTP error ({e.code}): {error_body}")

        except (TimeoutError, OSError) as e:
            last_error = e
            logger.warning(f"LLM network error on attempt {attempt + 1}: {type(e).__name__}: {e}")
            continue

        except Exception as e:
            last_error = e
            logger.warning(f"LLM unexpected error on attempt {attempt + 1}: {type(e).__name__}: {e}")
            continue

    # All attempts failed
    _llm_consecutive_failures += 1
    raise RuntimeError(f"LLM failed after 2 attempts: {last_error}")



# ─── Dispatcher + Reply Handler ───────────────────────────────────

dispatcher = Dispatcher(store=store, llm_fn=llm_complete)
reply_handler = ReplyHandler(store=store, llm_fn=llm_complete)

# ─── Pydantic Models ──────────────────────────────────────────────


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ─── Endpoints ────────────────────────────────────────────────────


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": store.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Prime",
        "team_members": ["Rithvik"],
        "model": LLM_MODEL or {"openai": "gpt-4o-mini", "anthropic": "claude-3-5-sonnet-20241022", "gemini": "gemini-1.5-flash", "deepseek": "deepseek-chat", "groq": "llama-3.1-70b-versatile"}.get(LLM_PROVIDER, f"{LLM_PROVIDER}-default"),
        "approach": "4-context composer with per-trigger-kind prompt routing, deterministic fallback, post-validation, and multi-turn reply handling",
        "contact_email": "rithvik@example.com",
        "version": "1.1.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    """Receive and store context push (trigger, merchant, category, customer)."""
    try:
        accepted, reason, current_version = store.push(
            body.scope, body.context_id, body.version, body.payload
        )

        if not accepted:
            if reason == "stale_version":
                logger.debug(f"Stale version for {body.scope}:{body.context_id} (current={current_version}, got={body.version})")
                return {"accepted": False, "reason": "stale_version", "current_version": current_version}
            logger.warning(f"Rejected {body.scope}:{body.context_id} — {reason}")
            return {"accepted": False, "reason": reason, "details": f"Invalid {reason}"}

        logger.debug(f"Stored {body.scope}:{body.context_id} v{body.version}")
        return {
            "accepted": True,
            "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": datetime.now(timezone.utc).isoformat() + "Z",
        }
    except Exception as e:
        logger.error(f"Error pushing context: {e}", exc_info=True)
        return {"accepted": False, "reason": "internal_error", "details": str(e)}


@app.post("/v1/tick")
async def tick(body: TickBody):
    """Process available triggers and compose engagement messages."""
    actions = []
    errors = []

    if not body.available_triggers:
        logger.info("Tick: no triggers available")
        return {"actions": []}

    for trigger_id in body.available_triggers:
        try:
            action = await dispatcher.compose_for_trigger(trigger_id)
            if action and action.get("body"):
                actions.append(action)
            else:
                logger.debug(f"No action for trigger {trigger_id}")
        except Exception as e:
            logger.error(f"Error composing for {trigger_id}: {e}", exc_info=True)
            errors.append({"trigger_id": trigger_id, "error": str(e)})
            continue

    logger.info(f"Tick: {len(body.available_triggers)} triggers → {len(actions)} actions, {len(errors)} errors")
    response = {"actions": actions}
    if errors and logger.isEnabledFor(logging.DEBUG):
        response["errors"] = errors
    return response


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    """Handle incoming merchant/customer replies."""
    try:
        if not body.merchant_id:
            logger.warning(f"Reply without merchant_id: conv={body.conversation_id}")
            return {
                "action": "send",
                "body": "I need to know which business this is about. Can you confirm?",
                "cta": "open_ended",
                "rationale": "Missing merchant context",
            }

        result = await reply_handler.handle_reply(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id,
            customer_id=body.customer_id,
            from_role=body.from_role,
            message=body.message,
            turn_number=body.turn_number,
        )
        logger.debug(f"Reply handled: {body.from_role} in {body.conversation_id} → {result.get('action')}")
        return result
    except Exception as e:
        logger.error(f"Reply handler error: {e}", exc_info=True)
        # Context-aware error fallback
        if body.from_role == "customer" and body.customer_id:
            customer = store.get_customer(body.customer_id)
            c_name = customer.get("identity", {}).get("name", "there") if customer else "there"
            return {
                "action": "send",
                "body": f"Got it, {c_name}! Let me check on that and get back to you shortly.",
                "cta": "open_ended",
                "rationale": "Error recovery fallback — addressing customer",
            }
        else:
            merchant = store.get_merchant(body.merchant_id) if body.merchant_id else None
            m_name = merchant.get("identity", {}).get("owner_first_name", "there") if merchant else "there"
            return {
                "action": "send",
                "body": f"Got it, {m_name} — let me check on that and get back to you.",
                "cta": "open_ended",
                "rationale": "Error recovery fallback — addressing merchant",
            }


# ─── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    model_name = LLM_MODEL or {"openai": "gpt-4o-mini", "anthropic": "claude-3-5-sonnet-20241022", "gemini": "gemini-1.5-flash", "deepseek": "deepseek-chat", "groq": "llama-3.1-70b-versatile"}.get(LLM_PROVIDER, "unknown")
    logger.info(f"Starting Vera Bot on port {port}")
    logger.info(f"LLM provider: {LLM_PROVIDER} / model: {model_name}")
    if LLM_API_KEY:
        logger.info(f"LLM_API_KEY configured ({len(LLM_API_KEY)} chars) — LLM composition enabled")
    else:
        logger.warning("LLM_API_KEY not set — bot will use deterministic fallback compositions (still functional)")
    uvicorn.run(app, host="0.0.0.0", port=port)
