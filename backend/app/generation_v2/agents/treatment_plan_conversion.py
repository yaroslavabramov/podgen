from __future__ import annotations

from app.generation_v2.agents.editorial_context import source_ref_context as _source_ref_context
from app.generation_v2.schemas import (
    EditorialAtom,
    ResearchMemo,
    SourceMap,
    Treatment,
    TreatmentAct,
    TreatmentAtomAnchor,
    TreatmentPlan,
    TreatmentPlanAct,
)


def treatment_atom_refs(research_memo: ResearchMemo) -> dict[str, EditorialAtom]:
    return {f"A{idx:02d}": atom for idx, atom in enumerate(research_memo.editorial_atoms, start=1)}


def resolve_treatment_source_focus(
    *,
    plan_act: TreatmentPlanAct,
    selected_atoms: list[EditorialAtom],
    source_inventory: SourceMap,
) -> list[str]:
    context = _source_ref_context(source_inventory.source_cards)
    known_source_ids = set(context.ref_by_source_id)
    source_focus: list[str] = []
    for value in plan_act.source_focus:
        text = str(value or "").strip()
        source_id = context.source_id_by_ref.get(text)
        if source_id is None and text in known_source_ids:
            source_id = text
        resolved_value = source_id or text
        if resolved_value and resolved_value not in source_focus:
            # Preserve invalid model handles so the deterministic treatment
            # validator can reject and repair them instead of silently hiding
            # the contract violation.
            source_focus.append(resolved_value)
    minimum = max(1, min(int(plan_act.source_density_target or 1), 2))
    for atom in selected_atoms:
        for source_id in atom.source_ids:
            if source_id and source_id not in source_focus:
                source_focus.append(source_id)
            if len(source_focus) >= minimum:
                break
        if len(source_focus) >= minimum:
            break
    return source_focus


def treatment_from_plan(
    plan: TreatmentPlan,
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap,
) -> Treatment:
    atom_by_ref = treatment_atom_refs(research_memo)
    acts: list[TreatmentAct] = []
    for plan_act in plan.acts:
        atom_refs = [str(ref).strip() for ref in plan_act.required_atom_refs if str(ref).strip()]
        selected_atoms = [atom_by_ref[ref] for ref in atom_refs if ref in atom_by_ref]
        evidence_ids: list[str] = []
        quote_refs: list[str] = []
        for atom in selected_atoms:
            for evidence_id in atom.evidence_ids:
                if evidence_id and evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
            for quote_ref in atom.quote_refs:
                quote_ref = str(quote_ref or "").strip()
                if quote_ref and quote_ref not in quote_refs:
                    quote_refs.append(quote_ref)
        acts.append(
            TreatmentAct(
                act_number=plan_act.act_number,
                title=plan_act.title,
                story_role=plan_act.story_role,
                opening_question=plan_act.opening_question,
                main_turn=plan_act.main_turn,
                ending_turn=plan_act.ending_turn,
                source_focus=resolve_treatment_source_focus(
                    plan_act=plan_act,
                    selected_atoms=selected_atoms,
                    source_inventory=source_inventory,
                ),
                target_minutes=plan_act.target_minutes,
                required_atom_refs=atom_refs,
                required_quote_refs=quote_refs,
                required_evidence_ids=evidence_ids,
                episode_supported_answer=research_memo.thesis,
                atom_anchors=[
                    TreatmentAtomAnchor(
                        atom_ref=atom_ref,
                        kind=atom.kind,
                        title=atom.title,
                        claim=atom.claim or atom.explanation,
                        story_value=atom.story_value,
                        story_weight=atom.story_weight,
                    )
                    for atom_ref, atom in zip(
                        [ref for ref in atom_refs if ref in atom_by_ref],
                        selected_atoms,
                        strict=True,
                    )
                ],
                source_density_target=plan_act.source_density_target,
            )
        )
    return Treatment(
        title=plan.title,
        logline=plan.logline,
        host_dynamic=plan.host_dynamic,
        supported_answer=research_memo.thesis,
        acts=acts,
        core_atom_dispositions=plan.core_atom_dispositions,
        recurring_questions=plan.recurring_questions,
        style_constraints=plan.style_constraints,
        planning_gaps=[str(item) for item in plan.planning_gaps if str(item).strip()],
    )


__all__ = [
    "resolve_treatment_source_focus",
    "treatment_atom_refs",
    "treatment_from_plan",
]
