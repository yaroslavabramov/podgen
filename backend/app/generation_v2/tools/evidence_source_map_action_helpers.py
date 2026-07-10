from __future__ import annotations

from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_CITED, EVIDENCE_STATUS_SELECTED
from app.generation_v2.tools.evidence_utils import _dedupe
from app.models.generation_v2 import EvidenceItem, V2ToolTrace


def trace_accepted(trace: V2ToolTrace) -> bool:
    output = getattr(trace, "output_json", None) or {}
    return bool(output.get("accepted", True))


def selected_evidence_ids_from_actions(traces: list[V2ToolTrace], evidence_by_id: dict[str, EvidenceItem]) -> list[str]:
    selected = [
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if (evidence.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
    ]
    for trace in traces:
        action = trace.input_json or {}
        if trace.tool_name == "select_evidence":
            evidence_id = str(action.get("evidence_id") or "")
            if evidence_id:
                selected.append(evidence_id)
            continue
        if trace.tool_name in {
            "upsert_source_card",
            "create_quote_card",
            "record_source_relationship",
            "record_source_cluster",
            "record_gap",
        }:
            selected.extend(str(eid) for eid in action.get("evidence_ids") or [] if str(eid) in evidence_by_id)
            evidence_id = str(action.get("evidence_id") or "")
            if evidence_id in evidence_by_id:
                selected.append(evidence_id)
    return _dedupe(selected)


def selected_role(evidence: EvidenceItem) -> str:
    metadata = evidence.metadata_ or {}
    return str(metadata.get("selected_role") or metadata.get("selected_purpose") or evidence.purpose or "background")


def relationship_kind(value: str) -> str:
    allowed = {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates", "background_for"}
    return value if value in allowed else "background_for"


def cluster_id(label: str, index: int) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())[:40].strip("_")
    return slug or f"cluster_{index}"


__all__ = [
    "cluster_id",
    "relationship_kind",
    "selected_evidence_ids_from_actions",
    "selected_role",
    "trace_accepted",
]
