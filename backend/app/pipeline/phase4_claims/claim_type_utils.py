"""Claim type normalization helpers."""

from __future__ import annotations

from typing import Any

VALID_CLAIM_TYPES = {"fact", "opinion", "report"}
_FACT_LIKE_CLAIM_TYPES = {
    "case",
    "anecdote",
    "timeline",
    "definition",
    "mechanism",
    "contrast",
    "statistic",
    "expert_quote",
    "expert-quote",
}


def normalize_claim_type(raw_claim_type: Any, attribution: dict | None = None) -> tuple[str, bool]:
    """Map LLM output into the only claim types the schema accepts."""
    normalized = str(raw_claim_type or "fact").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in VALID_CLAIM_TYPES:
        return normalized, False
    if normalized in _FACT_LIKE_CLAIM_TYPES:
        return "fact", True
    if isinstance(attribution, dict) and str(attribution.get("who") or "").strip():
        return "report", True
    return "fact", True
