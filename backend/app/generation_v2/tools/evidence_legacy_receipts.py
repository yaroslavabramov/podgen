from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.generation_v2.tools.evidence_source_passage_handles import (
    attach_source_passage_handles,
)
from app.generation_v2.tools.evidence_source_passages import source_passages_for_range


async def hydrate_legacy_source_receipts(
    owner: Any,
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> dict[str, Any]:
    """Replay old persisted read ranges from the pinned source, without a new read."""

    changed = False
    hydrated_receipt_refs: list[str] = []
    for receipt in workspace.get("source_read_receipts") or []:
        if (
            not isinstance(receipt, dict)
            or str(receipt.get("set_id") or "") != set_id
            or str(receipt.get("source_ref") or "") != source_ref
            or (receipt.get("passage_refs") and receipt.get("passage_handles"))
        ):
            continue
        version_id = str(receipt.get("source_version_id") or "")
        try:
            uuid.UUID(version_id)
        except (TypeError, ValueError):
            return _error(receipt, "legacy_receipt_missing_source_version")
        version = await owner._resolve_source_version(
            source_id="",
            source_version_id=version_id,
            source_ref=source_ref,
            version_ref="",
        )
        if version is None:
            return _error(receipt, "legacy_receipt_source_version_unavailable")
        text = _source_version_text(owner, version)
        content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        expected_sha256 = str(receipt.get("content_sha256") or "")
        if expected_sha256 and expected_sha256 != content_sha256:
            return _error(receipt, "legacy_receipt_source_content_changed")
        start_offset = int(receipt.get("start_offset") or 0)
        end_offset = int(receipt.get("end_offset") or 0)
        try:
            passages = source_passages_for_range(
                source_version_id=version.id,
                text=text,
                start_offset=start_offset,
                end_offset=end_offset,
                allow_timestamps=await owner._source_allows_timestamp_locators(version),
            )
        except (TypeError, ValueError):
            return _error(receipt, "legacy_receipt_range_invalid")
        receipt["passage_refs"] = [str(item["passage_ref"]) for item in passages]
        receipt["replay_passages"] = attach_source_passage_handles(
            workspace,
            receipt=receipt,
            passages=passages,
        )
        receipt["content_sha256"] = content_sha256
        changed = True
        hydrated_receipt_refs.append(str(receipt.get("receipt_ref") or ""))
    return {
        "accepted": True,
        "changed": changed,
        "hydrated_receipt_refs": hydrated_receipt_refs,
    }


def _source_version_text(owner: Any, version: Any) -> str:
    cached_getter = getattr(owner, "_cached_source_version_text", None)
    if callable(cached_getter):
        return str(cached_getter(version))
    from app.generation_v2.tools import evidence_retrieval

    return str(evidence_retrieval._source_version_text(version))


def _error(receipt: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "accepted": False,
        "error": code,
        "receipt_ref": str(receipt.get("receipt_ref") or ""),
        "next_action": "Read this source range again because the persisted legacy receipt cannot be replayed safely.",
    }


__all__ = ["hydrate_legacy_source_receipts"]
