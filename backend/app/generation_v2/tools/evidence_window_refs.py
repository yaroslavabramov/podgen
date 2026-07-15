from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass

from app.config import settings


_WINDOW_REF_PREFIX = "w1."
_WINDOW_REF_SIGNATURE_BYTES = 16


class WindowRefError(ValueError):
    pass


@dataclass(frozen=True)
class WindowRef:
    source_version_id: uuid.UUID
    start_offset: int
    end_offset: int
    text_fingerprint: str


def create_window_ref(
    *,
    source_version_id: uuid.UUID,
    text: str,
    start_offset: int,
    end_offset: int,
) -> str:
    if start_offset < 0 or end_offset <= start_offset or end_offset > len(text):
        raise WindowRefError("invalid_window_range")
    payload = {
        "e": end_offset,
        "f": _text_fingerprint(text),
        "s": start_offset,
        "v": str(source_version_id),
    }
    encoded_payload = _encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _encode(_sign(encoded_payload.encode("ascii")))
    return f"{_WINDOW_REF_PREFIX}{encoded_payload}.{signature}"


def parse_window_ref(value: str) -> WindowRef:
    text = str(value or "").strip()
    if not text.startswith(_WINDOW_REF_PREFIX):
        raise WindowRefError("invalid_window_ref")
    parts = text[len(_WINDOW_REF_PREFIX) :].split(".", 1)
    if len(parts) != 2:
        raise WindowRefError("invalid_window_ref")
    encoded_payload, encoded_signature = parts
    expected_signature = _sign(encoded_payload.encode("ascii"))
    try:
        supplied_signature = _decode(encoded_signature)
    except (ValueError, UnicodeError) as exc:
        raise WindowRefError("invalid_window_ref") from exc
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise WindowRefError("invalid_window_ref")
    try:
        payload = json.loads(_decode(encoded_payload).decode("utf-8"))
        parsed = WindowRef(
            source_version_id=uuid.UUID(str(payload["v"])),
            start_offset=int(payload["s"]),
            end_offset=int(payload["e"]),
            text_fingerprint=str(payload["f"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise WindowRefError("invalid_window_ref") from exc
    if parsed.start_offset < 0 or parsed.end_offset <= parsed.start_offset:
        raise WindowRefError("invalid_window_ref")
    return parsed


def resolve_window_ref(value: str, *, source_version_id: uuid.UUID, text: str) -> WindowRef:
    parsed = parse_window_ref(value)
    if parsed.source_version_id != source_version_id:
        raise WindowRefError("window_ref_version_mismatch")
    if parsed.text_fingerprint != _text_fingerprint(text):
        raise WindowRefError("window_ref_content_mismatch")
    if parsed.end_offset > len(text):
        raise WindowRefError("window_ref_out_of_bounds")
    return parsed


def is_window_ref(value: str) -> bool:
    return str(value or "").strip().startswith(_WINDOW_REF_PREFIX)


def _text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _sign(payload: bytes) -> bytes:
    key = hashlib.sha256(f"podgen:evidence-window:{settings.jwt_secret}".encode("utf-8")).digest()
    return hmac.new(key, payload, hashlib.sha256).digest()[:_WINDOW_REF_SIGNATURE_BYTES]


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
    if _encode(decoded) != value:
        raise ValueError("non_canonical_base64")
    return decoded


__all__ = [
    "WindowRef",
    "WindowRefError",
    "create_window_ref",
    "is_window_ref",
    "parse_window_ref",
    "resolve_window_ref",
]
