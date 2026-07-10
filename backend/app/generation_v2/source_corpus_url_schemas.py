from __future__ import annotations

from typing import Any

from app.generation_v2.source_corpus_contracts import OPENAI_URL_READ_SCHEMA_ENUM, OPENAI_URL_RECOVERY_SCHEMA_ENUM

OPENAI_URL_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "read_status": {
            "type": "string",
            "enum": OPENAI_URL_READ_SCHEMA_ENUM,
        },
        "title": {"type": "string"},
        "canonical_url": {"type": "string"},
        "content_type": {"type": "string"},
        "extracted_text_markdown": {"type": "string"},
        "summary": {"type": "string"},
        "failure_reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "read_status",
        "title",
        "canonical_url",
        "content_type",
        "extracted_text_markdown",
        "summary",
        "failure_reason",
        "confidence",
    ],
}

OPENAI_URL_RECOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recovery_status": {
            "type": "string",
            "enum": OPENAI_URL_RECOVERY_SCHEMA_ENUM,
        },
        "original_url_status": {"type": "string"},
        "best_alternative_url": {"type": "string"},
        "alternative_urls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["url", "title", "reason", "confidence"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["recovery_status", "original_url_status", "best_alternative_url", "alternative_urls", "notes"],
}

__all__ = ["OPENAI_URL_READ_SCHEMA", "OPENAI_URL_RECOVERY_SCHEMA"]
