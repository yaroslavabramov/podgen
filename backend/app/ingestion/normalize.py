"""Text normalization layer: cleans extracted text before chunking.

Applied after extraction and MinIO storage (raw text preserved),
before structural chunking and embedding.
"""

import logging
import re
import unicodedata

from app.models.source import SourceType

logger = logging.getLogger(__name__)


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_WEB_PDF_EXTRACTION_PROFILES = {"web_pdf_direct", "web_pdf_promoted"}


def normalize_text(text: str, source_type: str, *, extraction_profile: str | None = None) -> str:
    """Apply normalization transforms to extracted text.

    Universal transforms run for all source types.
    PDF-specific transforms run for PDF sources and web sources extracted from PDF.
    """
    if not text:
        return text

    original_len = len(text)

    # --- Universal transforms ---
    text = _unicode_nfc(text)
    text = _CONTROL_CHAR_RE.sub("", text)
    text = _normalize_whitespace(text)

    # --- PDF-specific transforms ---
    is_pdf_profile = (
        source_type == SourceType.PDF
        or (source_type == SourceType.WEB and extraction_profile in _WEB_PDF_EXTRACTION_PROFILES)
    )
    if is_pdf_profile:
        text = _fix_hyphenation(text)
        text = _strip_page_numbers(text)
        text = _remove_boilerplate(text)
        text = _normalize_whitespace(text)  # re-run after removals

    logger.debug(
        "Normalized text: %d -> %d chars (source_type=%s)",
        original_len, len(text), source_type,
    )
    return text


# --- Transform implementations ------------------------------------------------


def _unicode_nfc(text: str) -> str:
    """Normalize Unicode to NFC form for consistent matching."""
    return unicodedata.normalize("NFC", text)


def _normalize_whitespace(text: str) -> str:
    """Collapse exotic whitespace, excessive newlines, trailing spaces."""
    # Replace non-breaking spaces, zero-width chars with regular space
    text = re.sub(r"[\u00a0\u2007\u202f\u2060\ufeff]", " ", text)
    # Collapse runs of spaces/tabs (preserve newlines)
    text = re.sub(r"[^\S\n]+", " ", text)
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace from each line
    text = re.sub(r" +\n", "\n", text)
    return text.strip()


def _fix_hyphenation(text: str) -> str:
    """Rejoin words split by line-break hyphens in PDFs.

    Pattern: word char + hyphen + newline + optional space + word char.
    """
    return re.sub(r"(\w)-\n\s*(\w)", r"\1\2", text)


def _strip_page_numbers(text: str) -> str:
    """Remove standalone page number lines (e.g. '42', '— 3 —')."""
    return re.sub(
        r"^\s*[-\u2013\u2014]?\s*\d{1,4}\s*[-\u2013\u2014]?\s*$",
        "",
        text,
        flags=re.MULTILINE,
    )


def _remove_boilerplate(text: str, threshold: int = 3) -> str:
    """Remove lines repeated more than `threshold` times.

    Targets PDF headers, footers, copyright notices.
    Only removes short lines (< 120 chars) to avoid deleting
    legitimate repeated content like survey questions.
    """
    lines = text.split("\n")
    line_counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if stripped:
            line_counts[stripped] = line_counts.get(stripped, 0) + 1

    boilerplate = {
        line for line, count in line_counts.items()
        if count >= threshold and len(line) < 120
    }

    if boilerplate:
        logger.debug("Removing %d boilerplate patterns", len(boilerplate))

    return "\n".join(line for line in lines if line.strip() not in boilerplate)
