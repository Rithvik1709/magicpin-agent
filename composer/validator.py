"""
Post-composition validator.

Checks LLM output before returning to the judge.
"""

from __future__ import annotations
import re
from .resolver import ResolvedContext


def validate(composed: dict, ctx: ResolvedContext) -> tuple[bool, list[str]]:
    """
    Validate a composed message against context constraints.

    Returns (is_valid, list_of_issues).
    """
    issues = []
    body = composed.get("body", "")

    # 1. Body must be non-empty
    if not body or not body.strip():
        issues.append("empty_body")

    # 2. Body length check
    if len(body) > 1500:
        issues.append(f"body_too_long:{len(body)}")

    # 3. send_as correctness
    scope = ctx.trigger.get("scope", "merchant")
    expected_send_as = "merchant_on_behalf" if scope == "customer" else "vera"
    if composed.get("send_as") != expected_send_as:
        # Auto-fix this
        composed["send_as"] = expected_send_as
        issues.append(f"send_as_corrected_to_{expected_send_as}")

    # 4. CTA shape
    valid_ctas = {"binary_yes_stop", "open_ended", "none"}
    if composed.get("cta") not in valid_ctas:
        composed["cta"] = "open_ended"
        issues.append("cta_defaulted_to_open_ended")

    # 5. suppression_key
    if not composed.get("suppression_key"):
        composed["suppression_key"] = ctx.trigger.get("suppression_key", "")

    # 6. Taboo word check
    taboos = ctx.category.get("voice", {}).get("vocab_taboo", [])
    body_lower = body.lower()
    for taboo in taboos:
        if taboo.lower() in body_lower:
            issues.append(f"taboo_word_found:{taboo}")

    # 7. Rationale present
    if not composed.get("rationale"):
        composed["rationale"] = "Composed from category + merchant + trigger context"

    return len([i for i in issues if not i.startswith("send_as_corrected") and not i.startswith("cta_defaulted")]) == 0, issues
