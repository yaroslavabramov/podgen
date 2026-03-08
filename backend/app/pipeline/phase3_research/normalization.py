"""Shared text normalization for research retrieval and evidence dedupe."""

import re
import unicodedata

_ESCAPED_PUNCT_RE = re.compile(r"\\([^\w\s])")
_UNICODE_NORMALIZE = str.maketrans({
    "\u201c": '"',
    "\u201d": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2039": "'",
    "\u203a": "'",
    "\u2014": "-",
    "\u2013": "-",
    "\u2011": "-",
    "\u2212": "-",
    "\u2026": "...",
    "\u00ad": "",
    "\u200b": "",
    "\u200c": "",
    "\u200d": "",
    "\ufeff": "",
})


def normalize_research_text(value: str) -> str:
    """Normalize text for robust matching across source and LLM output."""
    deescaped = _ESCAPED_PUNCT_RE.sub(r"\1", value)
    nfkc = unicodedata.normalize("NFKC", deescaped)
    normalized = nfkc.translate(_UNICODE_NORMALIZE)
    return " ".join(normalized.split())
