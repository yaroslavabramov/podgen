from __future__ import annotations

from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_READS_PER_COMMIT_ADVISORY,
    RESEARCH_READS_PER_COMMIT_WARNING,
)
from app.generation_v2.research_workspace_helpers import active_atom_drafts as _active_atom_drafts

MAX_REQUIREMENT_FINDING_LEADS = 8
MAX_REQUIREMENT_GAP_LEADS = 4
MAX_QUESTION_FINDING_LEADS = 16
MAX_QUESTION_GAP_LEADS = 6
MAX_RECENT_OBSERVATIONS = 8
MAX_RECENT_ATOMS = 12
MAX_RECENT_RESEARCH_GAPS = 12
MAX_RECOMMENDED_SOURCE_REFS = 8


def compact_research_job_prompt_payload(
    workspace: dict[str, Any],
    job_id: str,
    *,
    source_map_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Requirement-first initial payload for one research leaf job.

    The full SourceMap/CorpusBrief lineage stays persisted in the workspace and
    pageable through tools. This payload only gives the agent the concrete
    requirement loop and compact source-mapping leads needed to start.
    """

    del source_map_summary  # kept for the public call signature; not model-facing.
    workspace = _prompt_workspace(workspace)
    job = _workspace_job(workspace, job_id) or {}
    question_ids = _str_list(job.get("coverage_question_ids"))
    source_findings = _dict_list(job.get("source_findings"))
    source_gaps = _dict_list(job.get("source_gaps"))
    question_level_finding_leads = _question_level_finding_leads(
        source_findings,
        question_ids=question_ids,
        limit=MAX_QUESTION_FINDING_LEADS,
    )
    question_level_gap_leads = _question_level_gap_leads(
        source_gaps,
        question_ids=question_ids,
        limit=MAX_QUESTION_GAP_LEADS,
    )
    requirements = _requirement_payloads(
        job,
        source_findings=source_findings,
        source_gaps=source_gaps,
    )
    recommended_source_refs = _recommended_source_refs(
        job,
        requirement_payloads=requirements,
        question_finding_leads=question_level_finding_leads,
        question_gap_leads=question_level_gap_leads,
    )
    allowed_source_refs = _str_list(
        job.get("allowed_source_pool") or job.get("source_refs") or job.get("corpus_handoff_source_refs")
    )
    payload = {
        "job": {
            "job_id": job.get("job_id"),
            "corpus_handoff_id": job.get("corpus_handoff_id"),
            "coverage_question_ids": question_ids,
            **_preview_fields("label", job.get("label") or "", 240),
            "story_job": job.get("story_job"),
            **_preview_fields("purpose", job.get("purpose") or "", 360),
            "priority": job.get("priority"),
            "status": job.get("status"),
            "attempt": int(job.get("attempt") or 0),
            "required_atom_kinds": _str_list(job.get("required_atom_kinds")),
            "questions": _question_payloads(job),
            "requirements": requirements,
            "question_level_leads": {
                "finding_leads": question_level_finding_leads,
                "gap_leads": question_level_gap_leads,
                "usage": (
                    "Use these only when a requirement has no explicit leads or when its explicit leads are "
                    "too weak, conflicted, or incomplete. Do not treat them as answered requirements."
                ),
            },
            "source_scope": {
                "recommended_source_refs": recommended_source_refs,
                "allowed_source_pool": {
                    "available": bool(allowed_source_refs),
                    "source_count": len(allowed_source_refs),
                    "usage": (
                        "Backend scope for targeted search/read tools. It is not a reading checklist; open extra "
                        "sources only for a concrete missing link, weak support, or conflict."
                    ),
                },
                "allow_broad_search": bool(job.get("allow_broad_search")),
            },
            "persisted_progress": _persisted_job_progress(workspace, job),
            "repair": _repair_payload(job),
        },
        "commit_advisory": {
            "reads_since_commit": int(workspace.get("reads_since_commit") or 0),
            "warning_threshold": RESEARCH_READS_PER_COMMIT_WARNING,
            "advisory_threshold": RESEARCH_READS_PER_COMMIT_ADVISORY,
            "commit_recommended": bool(workspace.get("commit_recommended")),
        },
        "pageable_context": {
            "source_findings": {
                "section": "current_job_source_findings",
                "usage": "Target by requirement_id and/or query; do not page the entire catalog.",
            },
            "source_gaps": {
                "section": "current_job_source_gaps",
                "usage": "Target by explicit requirement_id or a concrete unresolved subquestion.",
            },
            "observations": {"section": "current_job_observations"},
            "requirements": {"section": "job_requirement_outcomes"},
        },
    }
    if payload["job"]["repair"] is None:
        del payload["job"]["repair"]
    return payload


def _prompt_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    current = dict(workspace or {})
    current.setdefault("investigation_jobs", [])
    current.setdefault("atom_drafts", [])
    current.setdefault("research_gaps", [])
    return current


def _workspace_job(workspace: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    for item in workspace.get("investigation_jobs") or []:
        if isinstance(item, dict) and str(item.get("job_id") or "") == str(job_id):
            return dict(item)
    return None


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value or [] if str(item).strip()]


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = model_preview_text(value, collapse_whitespace=True)
    preview = text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"
    return {
        f"{name}_preview": preview,
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _preview_list(values: Any, *, item_limit: int, char_limit: int) -> list[dict[str, Any]]:
    return [_preview_fields("text", item, char_limit) for item in _str_list(values)[:item_limit]]


def _question_payloads(job: dict[str, Any]) -> list[dict[str, Any]]:
    questions = _str_list(job.get("questions"))
    ids = _str_list(job.get("coverage_question_ids"))
    if ids and len(ids) == len(questions):
        return [
            {"question_id": question_id, "question": question}
            for question_id, question in zip(ids, questions, strict=False)
        ]
    return [{"question_id": ids[index] if index < len(ids) else "", "question": question} for index, question in enumerate(questions)]


def _requirement_payloads(
    job: dict[str, Any],
    *,
    source_findings: list[dict[str, Any]],
    source_gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for requirement in _dict_list(job.get("coverage_requirements")):
        requirement_id = str(requirement.get("requirement_id") or "")
        question_id = str(requirement.get("question_id") or "")
        linked_finding_ids = set(_str_list((requirement.get("result") or {}).get("finding_ids")))
        finding_leads = [
            _finding_lead_payload(finding)
            for finding in source_findings
            if _finding_explicitly_links_requirement(
                finding,
                question_id=question_id,
                requirement_id=requirement_id,
                linked_finding_ids=linked_finding_ids,
            )
        ][:MAX_REQUIREMENT_FINDING_LEADS]
        gap_leads = [
            _gap_lead_payload(gap)
            for gap in source_gaps
            if _gap_explicitly_links_requirement(
                gap,
                question_id=question_id,
                requirement_id=requirement_id,
            )
        ][:MAX_REQUIREMENT_GAP_LEADS]
        payloads.append(
            {
                "question_id": question_id,
                "requirement_id": requirement_id,
                "description": str(requirement.get("description") or ""),
                "rationale": str(requirement.get("rationale") or ""),
                "completion_criterion": str(requirement.get("completion_criterion") or ""),
                "finding_leads": finding_leads,
                "residual_gap_leads": gap_leads,
                "current_result": _requirement_result_payload(requirement.get("result") or {}),
            }
        )
    return payloads


def _requirement_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "finding_ids": _str_list(result.get("finding_ids"))[:12],
        **_preview_fields("summary", result.get("summary") or "", 360),
        **_preview_fields("closure_basis", result.get("closure_basis") or "", 360),
        **_preview_fields("remaining_uncertainty", result.get("remaining_uncertainty") or "", 300),
        **_preview_fields("missing_detail", result.get("missing_detail") or "", 300),
        **_preview_fields("conflict_summary", result.get("conflict_summary") or "", 300),
    }


def _finding_explicitly_links_requirement(
    finding: dict[str, Any],
    *,
    question_id: str,
    requirement_id: str,
    linked_finding_ids: set[str],
) -> bool:
    finding_id = str(finding.get("finding_id") or "")
    if finding_id and finding_id in linked_finding_ids:
        return True
    for link in finding.get("question_links") or []:
        if not isinstance(link, dict):
            continue
        if question_id and str(link.get("question_id") or "") != question_id:
            continue
        if requirement_id in _str_list(link.get("requirement_ids")):
            return True
    return False


def _gap_explicitly_links_requirement(
    gap: dict[str, Any],
    *,
    question_id: str,
    requirement_id: str,
) -> bool:
    if not requirement_id or str(gap.get("requirement_id") or "") != requirement_id:
        return False
    gap_question_id = str(gap.get("question_id") or "")
    return not question_id or not gap_question_id or gap_question_id == question_id


def _question_level_finding_leads(
    findings: list[dict[str, Any]],
    *,
    question_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    allowed_questions = set(question_ids)
    if not allowed_questions:
        return []
    eligible = [
        finding
        for finding in findings
        if _finding_question_ids(finding).intersection(allowed_questions)
    ]
    selected: list[dict[str, Any]] = []
    seen_finding_ids: set[str] = set()
    seen_source_refs: set[str] = set()
    for finding in eligible:
        source_ref = str(finding.get("source_ref") or "")
        if source_ref and source_ref in seen_source_refs:
            continue
        selected.append(finding)
        if source_ref:
            seen_source_refs.add(source_ref)
        finding_id = str(finding.get("finding_id") or "")
        if finding_id:
            seen_finding_ids.add(finding_id)
        if len(selected) >= limit:
            return [_finding_lead_payload(item) for item in selected]
    for finding in eligible:
        finding_id = str(finding.get("finding_id") or "")
        if finding_id and finding_id in seen_finding_ids:
            continue
        selected.append(finding)
        if finding_id:
            seen_finding_ids.add(finding_id)
        if len(selected) >= limit:
            break
    return [_finding_lead_payload(item) for item in selected]


def _question_level_gap_leads(
    gaps: list[dict[str, Any]],
    *,
    question_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    allowed_questions = set(question_ids)
    if not allowed_questions:
        return []
    rows: list[dict[str, Any]] = []
    for gap in gaps:
        if str(gap.get("requirement_id") or ""):
            continue
        gap_question_id = str(gap.get("question_id") or "")
        if allowed_questions and gap_question_id and gap_question_id not in allowed_questions:
            continue
        rows.append(gap)
    return [_gap_lead_payload(gap) for gap in rows[:limit]]


def _finding_question_ids(finding: dict[str, Any]) -> set[str]:
    ids = {str(item) for item in finding.get("question_ids") or [] if str(item).strip()}
    ids.update(str(item) for item in finding.get("possible_question_ids") or [] if str(item).strip())
    for link in finding.get("question_links") or []:
        if isinstance(link, dict) and str(link.get("question_id") or "").strip():
            ids.add(str(link.get("question_id") or ""))
    return ids


def _finding_lead_payload(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": finding.get("finding_id"),
        "source_ref": finding.get("source_ref"),
        **_preview_fields("statement", finding.get("statement") or "", 520),
        "limitation_previews": _preview_list(finding.get("limitations"), item_limit=4, char_limit=220),
        "evidence_ids": _str_list(finding.get("evidence_ids")),
        "question_ids": sorted(_finding_question_ids(finding)),
    }


def _gap_lead_payload(gap: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_ref": gap.get("source_ref"),
        "question_id": gap.get("question_id"),
        **_preview_fields("description", gap.get("description") or "", 420),
        "evidence_ids": _str_list(gap.get("evidence_ids")),
    }
    requirement_id = str(gap.get("requirement_id") or "")
    if requirement_id:
        payload["requirement_id"] = requirement_id
    return payload


def _recommended_source_refs(
    job: dict[str, Any],
    *,
    requirement_payloads: list[dict[str, Any]],
    question_finding_leads: list[dict[str, Any]],
    question_gap_leads: list[dict[str, Any]],
) -> list[str]:
    refs: list[str] = _str_list(job.get("recommended_source_refs"))
    for requirement in requirement_payloads:
        for lead in requirement.get("finding_leads") or []:
            refs.append(str(lead.get("source_ref") or ""))
        for gap in requirement.get("residual_gap_leads") or []:
            refs.append(str(gap.get("source_ref") or ""))
    for lead in question_finding_leads:
        refs.append(str(lead.get("source_ref") or ""))
    for gap in question_gap_leads:
        refs.append(str(gap.get("source_ref") or ""))
    if not refs:
        refs = _str_list(job.get("source_refs"))
    return _dedupe(refs)[:MAX_RECOMMENDED_SOURCE_REFS]


def _persisted_job_progress(workspace: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    atoms = [
        item
        for item in _active_atom_drafts(workspace.get("atom_drafts") or [])
        if isinstance(item, dict) and str(item.get("job_id") or "") == job_id
    ]
    observations = _dict_list(job.get("observations"))
    research_gaps = [
        item
        for item in _dict_list(workspace.get("research_gaps"))
        if job_id in _str_list(item.get("job_ids")) or job_id == str(item.get("job_id") or "")
    ]
    return {
        "observations": [
            {
                "source_refs": _str_list(item.get("source_refs")),
                "evidence_ids": _str_list(item.get("evidence_ids")),
                "requirement_id": str(item.get("requirement_id") or ""),
                **_preview_fields("note", item.get("note") or "", 520),
                "pending_question_previews": _preview_list(
                    item.get("pending_questions"),
                    item_limit=4,
                    char_limit=180,
                ),
            }
            for item in observations[-MAX_RECENT_OBSERVATIONS:]
        ],
        "atoms": [
            {
                "atom_id": item.get("atom_id"),
                "kind": item.get("kind"),
                **_preview_fields("title", item.get("title") or "", 180),
                **_preview_fields("claim", item.get("claim") or "", 420),
                "evidence_ids": _str_list(item.get("evidence_ids")),
                "source_refs": _str_list(item.get("source_refs")),
                **_preview_fields("risk", item.get("risk") or "", 240),
            }
            for item in atoms[-MAX_RECENT_ATOMS:]
        ],
        "research_gaps": [
            {
                "gap_id": item.get("gap_id"),
                "job_id": str(item.get("job_id") or ""),
                "requirement_id": str(item.get("requirement_id") or ""),
                **_preview_fields("question", item.get("question") or "", 420),
                "status": item.get("status"),
                "category": item.get("category"),
                "severity": item.get("severity"),
                "treatment_relevance": item.get("treatment_relevance"),
                "source_refs": _str_list(item.get("source_refs")),
                "evidence_ids": _str_list(item.get("evidence_ids")),
                **_preview_fields("resolution_note", item.get("resolution_note") or "", 300),
            }
            for item in research_gaps[-MAX_RECENT_RESEARCH_GAPS:]
        ],
        "question_outcomes": [
            {
                "question": str(item.get("question") or ""),
                "status": item.get("status"),
                **_preview_fields("note", item.get("note") or "", 420),
                "evidence_ids": _str_list(item.get("evidence_ids")),
            }
            for item in _dict_list(job.get("question_outcomes"))
        ],
        "requirement_outcomes": [
            {
                "requirement_id": item.get("requirement_id"),
                "status": item.get("status"),
                **_preview_fields("note", item.get("note") or "", 420),
                "evidence_ids": _str_list(item.get("evidence_ids")),
                "supporting_atom_ids": _str_list(item.get("supporting_atom_ids")),
            }
            for item in _dict_list(job.get("requirement_outcomes"))
        ],
        "job_evidence_ids": _str_list(job.get("evidence_ids")),
        "atom_ids": _str_list(job.get("atom_ids")),
    }


def _repair_payload(job: dict[str, Any]) -> dict[str, Any] | None:
    repair_gap = job.get("repair_gap") if isinstance(job.get("repair_gap"), dict) else None
    repair_for_gap_id = str(job.get("repair_for_gap_id") or "")
    if repair_gap is None and not repair_for_gap_id:
        return None
    return {
        "repair_for_gap_id": repair_for_gap_id or repair_gap.get("gap_id"),
        "gap": (
            {
                "gap_id": repair_gap.get("gap_id"),
                **_preview_fields("question", repair_gap.get("question") or "", 520),
                "category": repair_gap.get("category"),
                "severity": repair_gap.get("severity"),
                "treatment_relevance": repair_gap.get("treatment_relevance"),
                "source_refs": _str_list(repair_gap.get("source_refs")),
                "evidence_ids": _str_list(repair_gap.get("evidence_ids")),
                **_preview_fields("resolution_note", repair_gap.get("resolution_note") or "", 360),
            }
            if repair_gap is not None
            else None
        ),
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = ["compact_research_job_prompt_payload"]
