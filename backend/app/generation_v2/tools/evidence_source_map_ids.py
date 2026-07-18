from __future__ import annotations

import hashlib
import json
from typing import Any

from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_utils import _dedupe


def source_map_evidence_ids(source_map: SourceMap) -> list[str]:
    ids = [*source_map.evidence_ids]
    ids.extend(eid for card in source_map.source_cards for eid in card.evidence_ids)
    ids.extend(quote.evidence_id for quote in source_map.quote_cards if quote.evidence_id)
    ids.extend(eid for item in source_map.source_triage.ranked_sources for eid in item.evidence_ids)
    ids.extend(eid for values in source_map.source_triage.top_evidence_by_function.values() for eid in values)
    ids.extend(eid for cluster in source_map.source_triage.source_clusters for eid in cluster.evidence_ids)
    ids.extend(eid for relation in source_map.source_triage.source_relationships for eid in relation.evidence_ids)
    ids.extend(eid for gap in source_map.gap_records for eid in gap.evidence_ids)
    ids.extend(eid for finding in source_map.source_findings for eid in finding.evidence_ids)
    ids.extend(
        eid
        for finding in source_map.source_findings
        for relation in finding.source_relations
        for eid in relation.evidence_ids
    )
    ids.extend(eid for outcome in source_map.coverage_outcomes for eid in outcome.evidence_ids)
    return _dedupe([str(item) for item in ids if item])


def source_map_source_ids(source_map: SourceMap) -> set[str]:
    ids = {card.source_id for card in source_map.source_cards}
    ids.update(quote.source_id for quote in source_map.quote_cards)
    ids.update(item.source_id for item in source_map.source_triage.ranked_sources)
    ids.update(source_id for values in source_map.source_triage.top_sources_by_function.values() for source_id in values)
    for cluster in source_map.source_triage.source_clusters:
        ids.update(cluster.source_ids)
        ids.update(cluster.primary_source_ids)
        ids.update(cluster.secondary_source_ids)
        ids.update(cluster.duplicate_source_ids)
    for relation in source_map.source_triage.source_relationships:
        ids.add(relation.source_id)
        ids.add(relation.related_source_id)
    for gap in source_map.gap_records:
        ids.update(gap.source_ids)
    ids.update(finding.source_id for finding in source_map.source_findings)
    ids.update(
        relation.related_source_id
        for finding in source_map.source_findings
        for relation in finding.source_relations
    )
    ids.update(source_id for outcome in source_map.coverage_outcomes for source_id in outcome.source_ids)
    return {str(item) for item in ids if item}


def source_map_version_ids(source_map: SourceMap) -> set[str]:
    ids = {version_id for card in source_map.source_cards for version_id in card.source_version_ids}
    ids.update(quote.source_version_id for quote in source_map.quote_cards if quote.source_version_id)
    ids.update(version_id for item in source_map.source_triage.ranked_sources for version_id in item.source_version_ids)
    ids.update(version_id for finding in source_map.source_findings for version_id in finding.source_version_ids)
    return {str(item) for item in ids if item}


def public_gap_ref(gap_id: str) -> str:
    """Return a stable public handle that does not shift when another gap closes."""

    digest = hashlib.sha256(str(gap_id).encode("utf-8")).hexdigest()[:10].upper()
    return f"G-{digest}"


def public_gap_ref_for_record(value: Any) -> str:
    """Return the same stable handle for typed and legacy gap records."""

    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    payload = payload if isinstance(payload, dict) else {}
    gap_id = str(payload.get("gap_id") or "").strip()
    if not gap_id:
        # Completed pre-v2 workspaces can lack gap_id. Derive an identity from
        # semantic lineage rather than list position so unrelated edits do not
        # renumber the model-facing reference.
        gap_id = "legacy:" + json.dumps(
            {
                "kind": str(payload.get("kind") or "gap"),
                "description": " ".join(str(payload.get("description") or "").split()),
                "source_ids": sorted(str(item) for item in payload.get("source_ids") or []),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return public_gap_ref(gap_id)


def source_map_record_fingerprint(kind: str, value: Any) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    encoded = json.dumps(
        {"kind": kind, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
