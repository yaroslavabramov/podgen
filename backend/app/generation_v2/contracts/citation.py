from __future__ import annotations

from enum import StrEnum


class DialogueSpeaker(StrEnum):
    A = "A"
    B = "B"


class DraftClaimType(StrEnum):
    FACTUAL = "factual"
    INTERPRETIVE = "interpretive"
    QUOTE = "quote"
    CAUSAL = "causal"
    STATISTICAL = "statistical"
    HISTORICAL = "historical"


class SourceNeed(StrEnum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    NONE = "none"


class CitationSupportLevel(StrEnum):
    SUPPORTED = "supported"
    WEAK = "weak"
    UNSUPPORTED = "unsupported"
    OVERSTRONG = "overstrong"
    NEEDS_ATTRIBUTION = "needs_attribution"


__all__ = ["CitationSupportLevel", "DialogueSpeaker", "DraftClaimType", "SourceNeed"]
