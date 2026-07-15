"""Make exact evidence identity location-aware.

Revision ID: a2c4e6f8b1d3
Revises: f1a3c5e7b9d2
Create Date: 2026-07-15 16:00:00.000000
"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a2c4e6f8b1d3"
down_revision: Union[str, None] = "f1a3c5e7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _rewrite_hashes(location_aware=True)


def downgrade() -> None:
    _rewrite_hashes(location_aware=False)


def _rewrite_hashes(*, location_aware: bool) -> None:
    evidence = sa.table(
        "evidence_items",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("generation_run_id", postgresql.UUID(as_uuid=True)),
        sa.column("corpus_revision", sa.Integer()),
        sa.column("attempt", sa.Integer()),
        sa.column("source_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("snippet_text", sa.Text()),
        sa.column("normalized_quote_text", sa.Text()),
        sa.column("locator_text", sa.Text()),
        sa.column("locator_type", sa.String()),
        sa.column("evidence_strength", sa.String()),
        sa.column("snippet_hash", sa.String()),
        sa.column("metadata", postgresql.JSONB()),
        sa.column("created_at", sa.DateTime()),
    )
    bind = op.get_bind()
    rows = list(bind.execute(sa.select(evidence).order_by(evidence.c.created_at, evidence.c.id)).mappings())
    # Free every final hash before rewriting so downgrade cannot collide when
    # an exact and a snippet-level record now share the same text.
    for row in rows:
        temporary_hash = hashlib.sha256(f"evidence-location-hash-migration:{row['id']}".encode("utf-8")).hexdigest()
        bind.execute(evidence.update().where(evidence.c.id == row["id"]).values(snippet_hash=temporary_hash))
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        canonical_hash = _canonical_hash(row, location_aware=location_aware)
        scope_key = (
            row["generation_run_id"],
            int(row["corpus_revision"] or 1),
            int(row["attempt"] or 1),
            row["source_version_id"],
            canonical_hash,
        )
        stored_hash = canonical_hash
        if scope_key in seen:
            stored_hash = hashlib.sha256(
                f"{canonical_hash}\x00location-migration-duplicate:{row['id']}".encode("utf-8")
            ).hexdigest()
        seen.add(scope_key)
        bind.execute(evidence.update().where(evidence.c.id == row["id"]).values(snippet_hash=stored_hash))


def _canonical_hash(row: dict[str, Any], *, location_aware: bool) -> str:
    is_exact = str(row["evidence_strength"] or "") == "exact"
    text = str(row["normalized_quote_text"] or "") if is_exact else ""
    if not text.strip():
        text = str(row["snippet_text"] or "")
    canonical_text = " ".join(unicodedata.normalize("NFKC", text).split())
    location_key = _location_key(row) if location_aware and is_exact else ""
    value = canonical_text if not location_key else f"{canonical_text}\x00location:{location_key}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _location_key(row: dict[str, Any]) -> str:
    metadata = dict(row["metadata"] or {})
    window_ref = str(metadata.get("window_ref") or "").strip()
    payload = _window_payload(window_ref)
    if payload is not None:
        return f"window:{payload.get('v')}:{payload.get('s')}:{payload.get('e')}:{payload.get('f')}"
    locator = " ".join(unicodedata.normalize("NFKC", str(row["locator_text"] or "")).split()).casefold()
    locator_type = str(row["locator_type"] or "unknown").casefold()
    return f"locator:{locator_type}:{locator}" if locator else ""


def _window_payload(window_ref: str) -> dict[str, Any] | None:
    if not window_ref.startswith("w1."):
        return None
    encoded = window_ref[3:].split(".", 1)[0]
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    required = {"v", "s", "e", "f"}
    return payload if isinstance(payload, dict) and required.issubset(payload) else None
