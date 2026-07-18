from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass

from app.config import settings


_PAGE_REF_PREFIX = "ep1."
_PAGE_REF_SIGNATURE_BYTES = 16


class EvidencePageRefError(ValueError):
    pass


@dataclass(frozen=True)
class EvidencePageRef:
    evidence_id: uuid.UUID
    start_offset: int
    end_offset: int
    text_fingerprint: str


def create_evidence_page_ref(
    *, evidence_id: uuid.UUID, text: str, start_offset: int, end_offset: int
) -> str:
    if start_offset < 0 or end_offset <= start_offset or end_offset > len(text):
        raise EvidencePageRefError("invalid_evidence_page_range")
    payload = {
        "e": end_offset,
        "f": _fingerprint(text),
        "i": str(evidence_id),
        "s": start_offset,
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{_PAGE_REF_PREFIX}{encoded}.{_encode(_sign(encoded.encode('ascii')))}"


def resolve_evidence_page_ref(value: str, *, evidence_id: uuid.UUID, text: str) -> EvidencePageRef:
    parsed = parse_evidence_page_ref(value)
    if parsed.evidence_id != evidence_id:
        raise EvidencePageRefError("evidence_page_ref_item_mismatch")
    if parsed.text_fingerprint != _fingerprint(text):
        raise EvidencePageRefError("evidence_page_ref_content_mismatch")
    if parsed.end_offset > len(text):
        raise EvidencePageRefError("evidence_page_ref_out_of_bounds")
    return parsed


def parse_evidence_page_ref(value: str) -> EvidencePageRef:
    text = str(value or "").strip()
    if not text.startswith(_PAGE_REF_PREFIX):
        raise EvidencePageRefError("invalid_evidence_page_ref")
    parts = text[len(_PAGE_REF_PREFIX) :].split(".", 1)
    if len(parts) != 2:
        raise EvidencePageRefError("invalid_evidence_page_ref")
    encoded, signature = parts
    try:
        supplied = _decode(signature)
    except (UnicodeError, ValueError) as exc:
        raise EvidencePageRefError("invalid_evidence_page_ref") from exc
    if not hmac.compare_digest(supplied, _sign(encoded.encode("ascii"))):
        raise EvidencePageRefError("invalid_evidence_page_ref")
    try:
        payload = json.loads(_decode(encoded).decode("utf-8"))
        parsed = EvidencePageRef(
            evidence_id=uuid.UUID(str(payload["i"])),
            start_offset=int(payload["s"]),
            end_offset=int(payload["e"]),
            text_fingerprint=str(payload["f"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidencePageRefError("invalid_evidence_page_ref") from exc
    if parsed.start_offset < 0 or parsed.end_offset <= parsed.start_offset:
        raise EvidencePageRefError("invalid_evidence_page_ref")
    return parsed


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _sign(payload: bytes) -> bytes:
    key = hashlib.sha256(f"podgen:evidence-page:{settings.jwt_secret}".encode("utf-8")).digest()
    return hmac.new(key, payload, hashlib.sha256).digest()[:_PAGE_REF_SIGNATURE_BYTES]


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
    if _encode(decoded) != value:
        raise ValueError("non_canonical_base64")
    return decoded


__all__ = [
    "EvidencePageRef",
    "EvidencePageRefError",
    "create_evidence_page_ref",
    "parse_evidence_page_ref",
    "resolve_evidence_page_ref",
]
