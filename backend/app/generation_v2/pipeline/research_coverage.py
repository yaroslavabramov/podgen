from __future__ import annotations

from typing import Any

from app.generation_v2.schemas import EditorialAtom, ResearchMemo, SourceMap


def research_story_coverage(memo: ResearchMemo, valid_evidence_ids: set[str]) -> dict[str, bool]:
    def grounded(atom: EditorialAtom) -> bool:
        return bool(atom.evidence_ids) and all(evidence_id in valid_evidence_ids for evidence_id in atom.evidence_ids)

    coverage = {
        "mechanism": False,
        "context": False,
        "change": False,
        "impact": False,
        "driver_tension": False,
        "open_question_risk": bool(memo.open_questions or memo.risk_register),
    }
    for atom in memo.editorial_atoms:
        text = f"{atom.title} {atom.explanation} {atom.story_value}".lower()
        if atom.kind == "mechanism" and grounded(atom):
            coverage["mechanism"] = True
        if (atom.kind == "context" or "origin" in text or "history" in text or "background" in text) and grounded(atom):
            coverage["context"] = True
        if atom.kind == "change" and grounded(atom):
            coverage["change"] = True
        if (atom.kind == "impact" or "tradeoff" in text or "trade-off" in text or "consequence" in text) and grounded(atom):
            coverage["impact"] = True
        if atom.kind in {"driver", "tension"} and grounded(atom):
            coverage["driver_tension"] = True
        if atom.kind == "open_question":
            coverage["open_question_risk"] = True
    return coverage


def research_source_map_cluster_coverage_issues(memo: ResearchMemo, source_map: SourceMap) -> list[str]:
    issues: list[str] = []
    if not source_map.source_triage.source_clusters:
        return issues
    risk_text = " ".join([*memo.open_questions, *memo.risk_register]).lower()
    atom_source_sets = [set(str(source_id) for source_id in atom.source_ids) for atom in memo.editorial_atoms]
    for cluster in source_map.source_triage.source_clusters:
        if not research_cluster_is_major(cluster):
            continue
        cluster_sources = set(str(source_id) for source_id in cluster.source_ids)
        if not cluster_sources:
            continue
        if any(cluster_sources & source_ids for source_ids in atom_source_sets):
            continue
        label = str(cluster.label or cluster.cluster_id or "source cluster")
        cluster_id = str(cluster.cluster_id or "")
        if (label and label.lower() in risk_text) or (cluster_id and cluster_id.lower() in risk_text):
            continue
        issues.append(f"SourceMap cluster not covered by any research atom or explicit risk: {label}.")
    return issues


def research_cluster_is_major(cluster: Any) -> bool:
    text = f"{getattr(cluster, 'cluster_id', '')} {getattr(cluster, 'label', '')} {getattr(cluster, 'rationale', '')}".lower()
    low_priority_terms = {
        "duplicate",
        "near-duplicate",
        "near duplicate",
        "mirror",
        "reprint",
        "noise",
        "noisy",
        "weak",
        "low-priority",
        "low priority",
        "deferred",
        "background",
        "supplemental",
    }
    if any(term in text for term in low_priority_terms):
        return False
    source_count = len(getattr(cluster, "source_ids", None) or [])
    return bool(source_count >= 2 or getattr(cluster, "primary_source_ids", None) or getattr(cluster, "evidence_ids", None))


__all__ = [
    "research_cluster_is_major",
    "research_source_map_cluster_coverage_issues",
    "research_story_coverage",
]
