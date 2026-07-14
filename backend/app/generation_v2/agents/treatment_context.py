from __future__ import annotations

from typing import Any

from app.generation_v2.agents.editorial_context import (
    dedupe_strings as _dedupe_strings,
    source_ref_context as _source_ref_context,
    trim as _trim,
)
from app.generation_v2.agents.treatment_cautions import (
    add_source_caution,
    source_note_ref_pair,
    treatment_relevant_caution,
    treatment_source_cautions,
)
from app.generation_v2.agents.treatment_plan_conversion import (
    resolve_treatment_source_focus,
    treatment_atom_refs,
    treatment_from_plan,
)
from app.generation_v2.schemas import (
    EditorialAtom,
    ResearchAudit,
    ResearchMemo,
    SourceCard,
    SourceMap,
)
from app.generation_v2.tools.evidence_refs import SourceRefContext, source_ref_for_id
from app.models.generation_run import GenerationRun


def source_refs_for_ids(source_ids: list[str], context: SourceRefContext) -> list[str]:
    refs: list[str] = []
    for source_id in source_ids:
        ref = source_ref_for_id(str(source_id), context)
        if ref != "S??":
            refs.append(ref)
    return refs


def compact_preview(value: Any, limit: int) -> dict[str, Any]:
    raw = str(value or "")
    return {
        "text": _trim(raw, limit),
        "complete": len(raw) <= limit,
        "original_chars": len(raw),
    }


def risk_for_treatment(risk: str) -> str:
    risk = str(risk or "").strip()
    if risk.lower() in {"low", "medium", "high"}:
        return ""
    if len(risk) < 55:
        return ""
    return risk


def treatment_atom_payload(atom: EditorialAtom, idx: int, context: SourceRefContext) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "atom_ref": f"A{idx:02d}",
        "kind": atom.kind,
        # Treatment has no research-read tools. These fields are already
        # bounded by the persisted research contract, so pass them whole.
        "title": atom.title,
        "claim": atom.claim or atom.explanation,
        "explanation": atom.explanation,
        "story_value": atom.story_value,
        "source_refs": source_refs_for_ids(atom.source_ids, context),
        "confidence": atom.confidence,
        "story_weight": atom.story_weight,
        "coverage": atom.source_coverage_status,
    }
    if atom.treatment_role_hint:
        payload["treatment_role_hint"] = atom.treatment_role_hint
    risk = risk_for_treatment(atom.risk)
    if risk:
        payload["caution"] = risk
    return payload


def treatment_referenced_source_refs(
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    research_audit: ResearchAudit | None,
    context: SourceRefContext,
) -> set[str]:
    refs: set[str] = set()
    for atom in research_memo.editorial_atoms:
        refs.update(source_refs_for_ids(atom.source_ids, context))
    if research_audit is not None:
        for issue in research_audit.issues:
            for value in issue.source_refs:
                text = str(value or "").strip()
                if not text:
                    continue
                if text in context.source_id_by_ref:
                    refs.add(text)
                elif text in context.ref_by_source_id:
                    refs.add(context.ref_by_source_id[text])
    refs.discard("S??")
    return refs


def treatment_source_context(
    source_cards: list[SourceCard],
    *,
    referenced_refs: set[str],
    ref_context: SourceRefContext,
) -> list[dict[str, Any]]:
    context_rows: list[dict[str, Any]] = []
    for card in source_cards:
        source_ref = source_ref_for_id(card.source_id, ref_context)
        if source_ref not in referenced_refs:
            continue
        row = {
            "source_ref": source_ref,
            "title": card.title,
            "domain": card.domain,
            "source_type": card.source_type,
        }
        hint = str(card.summary or "").strip()
        role_bits = [str(item) for item in card.useful_for if str(item or "").strip()]
        if role_bits:
            row["role_hints"] = role_bits
        elif hint:
            row["role_hint"] = hint
        context_rows.append(row)
    return context_rows


def treatment_audit_constraints(research_memo: ResearchMemo, research_audit: ResearchAudit | None) -> list[str]:
    constraints: list[str] = []
    constraints.extend(str(item) for item in research_memo.risk_register if item)
    constraints.extend(f"Open question: {item}" for item in research_memo.open_questions if item)
    if research_audit is not None:
        if research_audit.verdict:
            constraints.append(f"Audit verdict: {research_audit.verdict}")
        for issue in research_audit.issues:
            if issue.severity == "blocking":
                continue
            constraints.append(f"{issue.severity} {issue.issue_type}: {issue.description}")
    return _dedupe_strings(constraints)


def treatment_input(
    *,
    run: GenerationRun,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap,
    research_audit: ResearchAudit | None,
    act_count_range: tuple[int, int],
    repair_instructions: list[str] | None = None,
) -> dict[str, Any]:
    context = _source_ref_context(source_inventory.source_cards)
    referenced_refs = treatment_referenced_source_refs(
        research_memo=research_memo,
        source_map=source_map,
        research_audit=research_audit,
        context=context,
    )
    weight_rank = {"core": 0, "supporting": 1, "caveat": 2}
    indexed_atoms = sorted(
        list(enumerate(research_memo.editorial_atoms, start=1)),
        key=lambda item: (weight_rank.get(item[1].story_weight, 1), item[0]),
    )
    payload = {
        "episode_brief": {
            "topic": str((run.config or {}).get("user_prompt") or ""),
            # The persisted field is validated at 2400 chars, so this preview is
            # complete while remaining a separate model-facing contract.
            "supported_answer_preview": compact_preview(research_memo.thesis, 2400),
            "output_language": str((run.config or {}).get("language") or "en"),
            "duration_minutes": int((run.config or {}).get("duration_target", 1800)) // 60,
            "act_count_range": {"min": act_count_range[0], "max": act_count_range[1]},
        },
        "research_atoms": [treatment_atom_payload(atom, idx, context) for idx, atom in indexed_atoms],
        "source_context": treatment_source_context(
            source_inventory.source_cards,
            referenced_refs=referenced_refs,
            ref_context=context,
        ),
        "source_cautions": _full_source_cautions(
            treatment_source_cautions(
                source_map=source_map,
                source_inventory=source_inventory,
                referenced_refs=referenced_refs,
                ref_context=context,
            )
        ),
        "audit_constraints": treatment_audit_constraints(research_memo, research_audit),
    }
    if repair_instructions:
        # This is the only mechanical retry. It must see every violation.
        payload["repair_instructions"] = _dedupe_strings(repair_instructions)
    return payload


def _full_source_cautions(cautions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "category": item.get("category"),
            "source_refs": item.get("source_refs") or [],
            "note": str(item.get("note") or ""),
        }
        for item in cautions
    ]


__all__ = [
    "add_source_caution",
    "source_note_ref_pair",
    "treatment_relevant_caution",
    "resolve_treatment_source_focus",
    "treatment_atom_refs",
    "treatment_from_plan",
]
