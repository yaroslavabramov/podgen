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


def risk_for_treatment(risk: str) -> str:
    risk = _trim(risk, 360)
    if risk.lower() in {"low", "medium", "high"}:
        return ""
    if len(risk) < 55:
        return ""
    return risk


def treatment_atom_payload(atom: EditorialAtom, idx: int, context: SourceRefContext) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "atom_ref": f"A{idx:02d}",
        "kind": atom.kind,
        "title": atom.title,
        "explanation": _trim(atom.explanation, 700),
        "story_value": _trim(atom.story_value, 420),
        "source_refs": source_refs_for_ids(atom.source_ids, context),
        "confidence": atom.confidence,
        "story_weight": atom.story_weight,
        "coverage": atom.source_coverage_status,
    }
    if atom.treatment_role_hint:
        payload["treatment_role_hint"] = _trim(atom.treatment_role_hint, 360)
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
            "title": _trim(card.title, 220),
            "domain": card.domain,
            "source_type": card.source_type,
        }
        hint = _trim(card.summary, 260)
        role_bits = [_trim(item, 160) for item in card.useful_for[:3] if item]
        if role_bits:
            row["role_hint"] = "; ".join(role_bits)
        elif hint:
            row["role_hint"] = hint
        context_rows.append(row)
    return context_rows


def treatment_audit_constraints(research_memo: ResearchMemo, research_audit: ResearchAudit | None) -> list[str]:
    constraints: list[str] = []
    constraints.extend(_trim(item, 420) for item in research_memo.risk_register[:20] if item)
    constraints.extend(f"Open question: {_trim(item, 360)}" for item in research_memo.open_questions[:8] if item)
    if research_audit is not None:
        if research_audit.verdict:
            constraints.append(f"Audit verdict: {_trim(research_audit.verdict, 500)}")
        for issue in research_audit.issues[:20]:
            if issue.severity == "blocking":
                continue
            constraints.append(f"{issue.severity} {issue.issue_type}: {_trim(issue.description, 420)}")
    return _dedupe_strings(constraints)[:32]


def treatment_input(
    *,
    run: GenerationRun,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap,
    research_audit: ResearchAudit | None,
    act_count_range: tuple[int, int],
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
    return {
        "episode_brief": {
            "topic": str((run.config or {}).get("user_prompt") or "")[:700],
            "language": str((run.config or {}).get("language") or "en"),
            "duration_minutes": int((run.config or {}).get("duration_target", 1800)) // 60,
            "act_count_range": {"min": act_count_range[0], "max": act_count_range[1]},
        },
        "research_atoms": [treatment_atom_payload(atom, idx, context) for idx, atom in indexed_atoms],
        "source_context": treatment_source_context(
            source_inventory.source_cards,
            referenced_refs=referenced_refs,
            ref_context=context,
        ),
        "source_cautions": treatment_source_cautions(
            source_map=source_map,
            source_inventory=source_inventory,
            referenced_refs=referenced_refs,
            ref_context=context,
        ),
        "audit_constraints": treatment_audit_constraints(research_memo, research_audit),
    }


__all__ = [
    "add_source_caution",
    "source_note_ref_pair",
    "treatment_relevant_caution",
    "resolve_treatment_source_focus",
    "treatment_atom_refs",
    "treatment_from_plan",
]
