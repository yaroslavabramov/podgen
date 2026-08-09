from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.generation_v2.schemas import CoverageQuestion, ResearchHandoffItem, SourceCard


def build_source_mapping_research_handoff(
    *,
    questions: list[CoverageQuestion],
    workspace: dict[str, Any],
    source_cards_by_ref: dict[str, SourceCard],
) -> list[ResearchHandoffItem]:
    """Build one deterministic research handoff per leaf question from SourceAnalysis records."""

    questions_by_id = {item.question_id: item for item in questions}
    leaf_question_ids = _leaf_question_ids(questions)
    findings_by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    gaps_by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    evidence_by_question: dict[str, list[str]] = defaultdict(list)
    source_refs_by_question: dict[str, list[str]] = defaultdict(list)
    source_cards_by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)

    analyses = _current_accepted_analyses(workspace) or _legacy_analyses_from_workspace(workspace)
    for analysis in analyses:
        source_ref = str(analysis.get("source_ref") or "")
        if not source_ref:
            continue
        card = source_cards_by_ref.get(source_ref)
        source_card_payload = _source_card_payload(card, source_ref=source_ref)
        for finding in analysis.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            finding_question_ids = [
                str(question_id)
                for question_id in finding.get("question_ids") or []
                if str(question_id) in leaf_question_ids
            ]
            if not finding_question_ids:
                continue
            finding_payload = {
                "finding_id": str(finding.get("finding_id") or ""),
                "source_ref": source_ref,
                "statement": str(finding.get("statement") or ""),
                "evidence_ids": _dedupe([str(item) for item in finding.get("evidence_ids") or [] if str(item)]),
                "question_ids": finding_question_ids,
                "limitations": [str(item) for item in finding.get("limitations") or [] if str(item)],
                "source_summary": str(analysis.get("summary") or ""),
                "source_limitations": [
                    str(item) for item in analysis.get("source_limitations") or [] if str(item)
                ],
            }
            for question_id in finding_question_ids:
                findings_by_question[question_id].append(finding_payload)
                source_refs_by_question[question_id].append(source_ref)
                source_cards_by_question[question_id].append(source_card_payload)
                evidence_by_question[question_id].extend(finding_payload["evidence_ids"])
        for gap in analysis.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            question_id = str(gap.get("question_id") or "")
            if question_id not in leaf_question_ids:
                continue
            gaps_by_question[question_id].append(
                {
                    "source_ref": source_ref,
                    "question_id": question_id,
                    "description": str(gap.get("description") or ""),
                    "source_summary": str(analysis.get("summary") or ""),
                }
            )
            source_refs_by_question[question_id].append(source_ref)
            source_cards_by_question[question_id].append(source_card_payload)
        for question_id in analysis.get("coverage_question_ids") or []:
            question_id = str(question_id)
            if question_id not in leaf_question_ids:
                continue
            source_refs_by_question[question_id].append(source_ref)
            source_cards_by_question[question_id].append(source_card_payload)

    handoff: list[ResearchHandoffItem] = []
    for question_id in sorted(leaf_question_ids, key=lambda item: _question_rank(questions, item)):
        question = questions_by_id[question_id]
        evidence_ids = _dedupe(evidence_by_question.get(question_id, []))
        source_refs = _dedupe(source_refs_by_question.get(question_id, []))
        handoff.append(
            ResearchHandoffItem(
                handoff_id=f"handoff:{question_id}",
                priority=str(question.importance),
                objective=question.question,
                question=question.question,
                question_ids=[question_id],
                question_assignments=[
                    {
                        "question_id": question_id,
                        "requirement_ids": [
                            requirement.requirement_id
                            for requirement in question.answer_requirements
                        ],
                    }
                ],
                requirements=[
                    requirement
                    for requirement in question.answer_requirements
                ],
                findings=_dedupe_dicts(findings_by_question.get(question_id, []), key="finding_id"),
                gaps=_dedupe_dicts(gaps_by_question.get(question_id, []), key=("source_ref", "question_id", "description")),
                evidence_ids=evidence_ids,
                source_refs=source_refs,
                source_cards=_dedupe_dicts(source_cards_by_question.get(question_id, []), key="source_ref"),
                reason="Deterministic source-mapping handoff for one leaf question.",
            )
        )
    return handoff


def _current_accepted_analyses(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in workspace.get("source_analyses") or []
        if isinstance(item, dict)
        and item.get("accepted") is not False
        and item.get("current") is not False
        and str(item.get("status") or "") == "usable"
    ]


def _legacy_analyses_from_workspace(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    assessments = [
        item for item in workspace.get("source_assessments") or [] if isinstance(item, dict)
    ]
    findings_by_source_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_ref_by_id = {
        str(item.get("source_id") or ""): str(item.get("source_ref") or "")
        for item in assessments
        if str(item.get("source_id") or "") and str(item.get("source_ref") or "")
    }
    for finding in workspace.get("source_findings") or []:
        if not isinstance(finding, dict):
            continue
        source_ref = str(finding.get("source_ref") or "") or source_ref_by_id.get(str(finding.get("source_id") or ""), "")
        if not source_ref:
            continue
        question_ids = _dedupe(
            [
                *[
                    str(link.get("question_id") or "")
                    for link in finding.get("question_links") or []
                    if isinstance(link, dict)
                ],
                *[str(item) for item in finding.get("possible_question_ids") or []],
            ]
        )
        findings_by_source_ref[source_ref].append(
            {
                "finding_id": str(finding.get("finding_id") or ""),
                "statement": str(finding.get("statement") or ""),
                "evidence_ids": _dedupe([str(item) for item in finding.get("evidence_ids") or [] if str(item)]),
                "question_ids": question_ids,
                "limitations": [str(item) for item in finding.get("limitations") or [] if str(item)],
            }
        )
    deferred_refs = {
        str(source_ref)
        for source_set in workspace.get("source_sets") or []
        if isinstance(source_set, dict) and str(source_set.get("status") or "") == "deferred"
        for source_ref in source_set.get("source_refs") or []
    }
    analyses: list[dict[str, Any]] = []
    for assessment in assessments:
        source_ref = str(assessment.get("source_ref") or "")
        if not source_ref or source_ref in deferred_refs:
            continue
        if str(assessment.get("disposition") or "") == "deferred":
            continue
        if str(assessment.get("representation_status") or "") in {"unreadable", "not_target_content"}:
            continue
        analyses.append(
            {
                "analysis_id": str(assessment.get("analysis_id") or assessment.get("assessment_id") or ""),
                "source_ref": source_ref,
                "source_id": str(assessment.get("source_id") or ""),
                "status": "usable",
                "summary": str(assessment.get("source_summary") or ""),
                "source_limitations": list(assessment.get("limitations") or []),
                "findings": findings_by_source_ref.get(source_ref, []),
                "gaps": [],
                "coverage_question_ids": [
                    str(item) for item in assessment.get("coverage_question_ids") or [] if str(item)
                ],
            }
        )
    return analyses


def _leaf_question_ids(questions: list[CoverageQuestion]) -> set[str]:
    question_ids = {item.question_id for item in questions}
    parent_ids = {item.parent_question_id for item in questions if item.parent_question_id}
    return question_ids - parent_ids or question_ids


def _question_rank(questions: list[CoverageQuestion], question_id: str) -> int:
    for index, question in enumerate(questions):
        if question.question_id == question_id:
            return index
    return len(questions)


def _source_card_payload(card: SourceCard | None, *, source_ref: str) -> dict[str, Any]:
    if card is None:
        return {"source_ref": source_ref}
    return {
        "source_ref": source_ref,
        "source_id": card.source_id,
        "title": card.title,
        "source_type": card.source_type,
        "url": card.url,
        "domain": card.domain,
        "published_at": card.published_at,
        "summary": card.summary,
        "gaps_or_cautions": list(card.gaps_or_cautions),
    }


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def _dedupe_dicts(values: list[dict[str, Any]], *, key: str | tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(key, tuple):
            marker = "\x1f".join(str(value.get(part) or "") for part in key)
        else:
            marker = str(value.get(key) or "")
        if not marker:
            marker = str(sorted(value.items()))
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


__all__ = ["build_source_mapping_research_handoff"]
