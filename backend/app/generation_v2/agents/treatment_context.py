from __future__ import annotations

from typing import Any

from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.agents.editorial_context import (
    dedupe_strings as _dedupe_strings,
    source_ref_context as _source_ref_context,
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
from app.generation_v2.tools.evidence_source_map_ids import source_map_evidence_ids
from app.models.generation_run import GenerationRun


def source_refs_for_ids(source_ids: list[str], context: SourceRefContext) -> list[str]:
    refs: list[str] = []
    for source_id in source_ids:
        ref = source_ref_for_id(str(source_id), context)
        if ref != "S??":
            refs.append(ref)
    return refs


def treatment_model_ref_replacements(context: SourceRefContext) -> dict[str, str]:
    replacements = dict(context.ref_by_source_id)
    for source_id, card in context.card_by_source_id.items():
        source_ref = context.ref_by_source_id.get(source_id)
        if source_ref is None:
            continue
        for internal_id in [
            card.source_profile_id,
            *card.source_version_ids,
            *card.representation_ids,
        ]:
            if internal_id:
                replacements[str(internal_id)] = source_ref
    return replacements


def add_treatment_evidence_ref_replacements(
    replacements: dict[str, str],
    *,
    source_map: SourceMap,
    source_inventory: SourceMap,
    evidence_records: list[dict[str, Any]] | None,
) -> None:
    """Replace only evidence ids known to this run with neutral public refs."""

    known_evidence_ids = {
        *source_map_evidence_ids(source_map),
        *source_map_evidence_ids(source_inventory),
        *(
            str(item.get("id"))
            for item in evidence_records or []
            if str(item.get("id") or "").strip()
        ),
    }
    next_index = 1
    for evidence_id in sorted(known_evidence_ids):
        if evidence_id in replacements:
            continue
        replacements[evidence_id] = f"E{next_index:03d}"
        next_index += 1


def risk_for_treatment(risk: str) -> str:
    risk = str(risk or "").strip()
    if risk.lower() in {"low", "medium", "high"}:
        return ""
    return risk


def treatment_atom_payload(
    atom: EditorialAtom,
    idx: int,
    context: SourceRefContext,
    quote_ref_by_evidence_id: dict[str, str],
    replacements: dict[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "atom_ref": f"A{idx:02d}",
        "kind": atom.kind,
        # Treatment has no research-read tools. These fields are already
        # bounded by the persisted research contract, so pass them whole.
        "title": model_preview_text(atom.title, replacements=replacements, collapse_whitespace=False),
        "claim": model_preview_text(
            atom.claim or atom.explanation,
            replacements=replacements,
            collapse_whitespace=False,
        ),
        "explanation": model_preview_text(
            atom.explanation,
            replacements=replacements,
            collapse_whitespace=False,
        ),
        "story_value": model_preview_text(
            atom.story_value,
            replacements=replacements,
            collapse_whitespace=False,
        ),
        "source_refs": source_refs_for_ids(atom.source_ids, context),
        "confidence": atom.confidence,
        "story_weight": atom.story_weight,
        "coverage": atom.source_coverage_status,
        "selected_quote_refs": [
            quote_ref_by_evidence_id[quote_ref]
            for quote_ref in atom.quote_refs
            if quote_ref in quote_ref_by_evidence_id
        ],
    }
    if atom.treatment_role_hint:
        payload["treatment_role_hint"] = model_preview_text(
            atom.treatment_role_hint,
            replacements=replacements,
            collapse_whitespace=False,
        )
    risk = risk_for_treatment(atom.risk)
    if risk:
        payload["caution"] = model_preview_text(
            risk,
            replacements=replacements,
            collapse_whitespace=False,
        )
    return payload


def treatment_selected_quotes(
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    context: SourceRefContext,
    evidence_records: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    cards_by_evidence_id = {
        str(card.evidence_id): card for card in source_map.quote_cards if str(card.evidence_id or "").strip()
    }
    evidence_by_id = {
        str(item.get("id")): item for item in evidence_records or [] if str(item.get("id") or "").strip()
    }
    ordered_evidence_ids: list[str] = []
    for atom in research_memo.editorial_atoms:
        for quote_ref in atom.quote_refs:
            quote_ref = str(quote_ref or "").strip()
            if (
                quote_ref in cards_by_evidence_id or quote_ref in evidence_by_id
            ) and quote_ref not in ordered_evidence_ids:
                ordered_evidence_ids.append(quote_ref)
    ref_by_evidence_id: dict[str, str] = {}
    catalog: list[dict[str, Any]] = []
    for evidence_id in ordered_evidence_ids:
        card = cards_by_evidence_id.get(evidence_id)
        evidence = evidence_by_id.get(evidence_id) or {}
        source_id = card.source_id if card is not None else str(evidence.get("source_id") or "")
        source_ref = source_ref_for_id(source_id, context)
        if source_ref == "S??":
            continue
        evidence_metadata = evidence.get("metadata") or {}
        quote_text = (
            str(card.quote or "")
            if card is not None
            else str(evidence_metadata.get("quote_text") or evidence.get("snippet_text") or "")
        )
        if not quote_text.strip():
            continue
        quote_ref = f"Q{len(catalog) + 1:02d}"
        ref_by_evidence_id[evidence_id] = quote_ref
        replacements = {
            evidence_id: quote_ref,
            source_id: source_ref,
        }
        for internal_id in (
            card.source_version_id if card is not None else None,
            card.representation_id if card is not None else None,
            evidence.get("source_version_id"),
        ):
            if internal_id:
                replacements[str(internal_id)] = source_ref
        catalog.append(
            {
                "quote_ref": quote_ref,
                "source_ref": source_ref,
                "locator": model_preview_text(
                    card.locator if card is not None else evidence.get("locator_text"),
                    replacements=replacements,
                    collapse_whitespace=False,
                ),
                # Treatment has no evidence-read tools. Pass the selected exact
                # quote whole; the upstream write contract already validates it.
                "exact_quote": model_preview_text(
                    quote_text,
                    replacements=replacements,
                    collapse_whitespace=False,
                ),
                "narrative_use": model_preview_text(
                    card.narrative_use if card is not None else "",
                    replacements=replacements,
                    collapse_whitespace=False,
                ),
            }
        )
    return catalog, ref_by_evidence_id


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
        replacements = {
            str(card.source_id): source_ref,
            **{
                str(internal_id): source_ref
                for internal_id in [
                    card.source_profile_id,
                    *card.source_version_ids,
                    *card.representation_ids,
                ]
                if internal_id
            },
        }
        row = {
            "source_ref": source_ref,
            "title": model_preview_text(
                card.title,
                replacements=replacements,
                collapse_whitespace=False,
            ),
            "domain": card.domain,
            "source_type": card.source_type,
        }
        hint = model_preview_text(
            card.summary,
            replacements=replacements,
            collapse_whitespace=False,
        )
        role_bits = [
            model_preview_text(item, replacements=replacements, collapse_whitespace=False)
            for item in card.useful_for
            if str(item or "").strip()
        ]
        if role_bits:
            row["role_hints"] = role_bits
        elif hint.strip():
            # There is no paging/read surface in treatment, so a partial source
            # role hint would be unrecoverable.
            row["role_hint"] = hint
        context_rows.append(row)
    return context_rows


def treatment_audit_constraints(
    research_memo: ResearchMemo,
    research_audit: ResearchAudit | None,
    *,
    replacements: dict[str, str],
) -> list[str]:
    constraints: list[str] = []
    constraints.extend(str(item) for item in research_memo.risk_register if item)
    constraints.extend(f"Open question: {item}" for item in research_memo.open_questions if item)
    if research_audit is not None:
        for issue in research_audit.issues:
            if issue.severity == "blocking":
                continue
            constraints.append(f"{issue.severity} {issue.issue_type}: {issue.description}")
    return _dedupe_strings(
        model_preview_text(item, replacements=replacements, collapse_whitespace=False)
        for item in constraints
    )


def treatment_input(
    *,
    run: GenerationRun,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap,
    research_audit: ResearchAudit | None,
    act_count_range: tuple[int, int],
    repair_instructions: list[str] | None = None,
    evidence_records: list[dict[str, Any]] | None = None,
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
    selected_quotes, quote_ref_by_evidence_id = treatment_selected_quotes(
        research_memo=research_memo,
        source_map=source_map,
        context=context,
        evidence_records=evidence_records,
    )
    model_ref_replacements = treatment_model_ref_replacements(context)
    for atom_index, atom in enumerate(research_memo.editorial_atoms, start=1):
        atom_ref = f"A{atom_index:02d}"
        for internal_id in atom.evidence_ids:
            if internal_id:
                model_ref_replacements.setdefault(str(internal_id), atom_ref)
    model_ref_replacements.update(quote_ref_by_evidence_id)
    add_treatment_evidence_ref_replacements(
        model_ref_replacements,
        source_map=source_map,
        source_inventory=source_inventory,
        evidence_records=evidence_records,
    )
    payload = {
        "episode_brief": {
            "topic": str((run.config or {}).get("user_prompt") or ""),
            # Treatment has no research-workspace reader. Preserve the complete
            # supported answer that research persisted.
            "supported_answer": model_preview_text(
                research_memo.thesis,
                replacements=model_ref_replacements,
                collapse_whitespace=False,
            ),
            "output_language": str((run.config or {}).get("language") or "en"),
            "duration_minutes": int((run.config or {}).get("duration_target", 1800)) // 60,
            "act_count_range": {"min": act_count_range[0], "max": act_count_range[1]},
        },
        "research_atoms": [
            treatment_atom_payload(
                atom,
                idx,
                context,
                quote_ref_by_evidence_id,
                model_ref_replacements,
            )
            for idx, atom in indexed_atoms
        ],
        # This is the complete research-status handoff, not a preview. Treatment
        # has no workspace reader, so partial/deferred findings and gaps must
        # remain available verbatim instead of being truncated or filtered out.
        "source_coverage": [
            model_preview_text(
                item,
                replacements=model_ref_replacements,
                collapse_whitespace=False,
            )
            for item in research_memo.source_coverage
        ],
        "selected_quotes": selected_quotes,
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
            ),
            replacements=model_ref_replacements,
        ),
        "audit_constraints": treatment_audit_constraints(
            research_memo,
            research_audit,
            replacements=model_ref_replacements,
        ),
    }
    if repair_instructions:
        # This is the only mechanical retry. It must see every violation.
        payload["repair_instructions"] = _dedupe_strings(
            model_preview_text(item, replacements=model_ref_replacements, collapse_whitespace=False)
            for item in repair_instructions
        )
    return payload


def _full_source_cautions(
    cautions: list[dict[str, Any]],
    *,
    replacements: dict[str, str],
) -> list[dict[str, Any]]:
    return [
        {
            "category": item.get("category"),
            "source_refs": item.get("source_refs") or [],
            "note": model_preview_text(
                item.get("note"),
                replacements=replacements,
                collapse_whitespace=False,
            ),
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
