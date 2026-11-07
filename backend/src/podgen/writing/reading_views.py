"""Allowlisted cold input, independently sourced fact input, and complete attributed feedback."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .craft_reference import attach_reference
from .duration_views import duration_context
from .projection_versions import CURRENT, creative_materials, historical, script_handoff
from .projections import (
    actionable_feedback,
    assignment_view,
    creative_knowledge_view,
    evidence_view,
    indexed_draft,
    knowledge_view,
    validate_model_content,
)
from .story_views import editorial_frame, selected_labels


def reading_payload(
    stage,
    materials,
    *,
    script,
    readings=None,
    decision=None,
    approved_plan=None,
    projection_version=CURRENT,
    craft_reference=None,
    revision_operation=None,
    composition=None,
):
    packet = materials.packet
    payload = dict(
        script=indexed_draft(script),
        audience=packet.assignment.listener_profile
        or "General listeners; no prior topic knowledge is assumed.",
        format=materials.format,
        genre=packet.assignment.genre or "podcast",
        output_language=materials.output_language,
    )
    if script_handoff(projection_version) and stage in ("reader-review", "revise"):
        payload["duration_context"] = duration_context(
            packet.assignment, script, include_requested=stage == "reader-review"
        )
    if stage == "reader-review":
        return payload
    if stage not in ("fact-review", "revise"):
        raise ContractError("unknown_reading_stage")
    if stage == "fact-review":
        for field in ("audience", "genre", "format"):
            payload.pop(field)
        assignment = packet.assignment
        payload["factual_remit"] = dict(
            question=assignment.question,
            required_topics=list(assignment.required_topics),
            knowledge_cutoff=assignment.knowledge_cutoff,
        )
        if assignment.excluded_topics:
            payload["factual_remit"]["excluded_topics"] = list(assignment.excluded_topics)
        payload.update(
            support=knowledge_view(packet, labels=True),
            evidence=evidence_view(packet),
            necessary_limits=list(packet.dossier.throughline_constraints),
        )
    else:
        if readings is None or set(readings) != {"reader", "fact"}:
            raise ContractError("independent_readings_required")
        if decision is None or decision["recommendation"] != "revise":
            raise ContractError("revision_decision_required")
        plan = approved_plan or (materials.fixed.plan.model_dump(mode="json") if materials.fixed else None)
        selected = selected_labels(plan, packet) if plan else set()
        selected = set(selected)
        needed_labels = {
            label for finding in readings["fact"]["findings"] for label in finding["support_labels"]
        }
        selected.update(needed_labels)
        payload["assignment"] = assignment_view(packet)
        if plan:
            payload["editorial_frame"] = editorial_frame(plan, projection_version=projection_version)
            if "revision_scope" in readings["reader"]:
                payload["editorial_frame"]["authority"] = "revisable_authorial_decisions"
        if composition is not None:
            questions = deepcopy(payload.get("editorial_frame", {}).get("unresolved_questions", []))
            payload["editorial_frame"] = dict(
                authority="revisable_authorial_decisions", unresolved_questions=questions,
                **{key: deepcopy(value) for key, value in composition.items() if key != "unresolved_questions"},
            )
            if "unresolved_questions" in composition:
                payload["editorial_frame"]["unresolved_questions"] = deepcopy(composition["unresolved_questions"])
        if revision_operation is not None:
            payload["revision_operation"] = deepcopy(revision_operation)
        payload["revision_guidance"] = [
            dict(
                origin=origin, **actionable_feedback(readings[origin], projection_version=projection_version)
            )
            for origin in ("reader", "fact")
        ]
        if revision_operation is not None:
            for guidance in payload["revision_guidance"]:
                if guidance["origin"] == "fact":
                    for field in ("listener_understanding", "duration_note", "important_limits"):
                        guidance.pop(field, None)
        if creative_materials(projection_version):
            payload["support"] = creative_knowledge_view(packet, labels=False, selected=selected)
        else:
            payload["support"] = knowledge_view(
                packet, labels=packet.contract != "editorial-input/2", selected=selected
            )
        if packet.contract == "editorial-input/2":
            passages = evidence_view(packet)
            for guidance in payload["revision_guidance"]:
                if guidance["origin"] != "fact":
                    continue
                for finding in guidance["findings"]:
                    references = set(finding.pop("support_labels"))
                    finding["evidence"] = [
                        dict(source=passage["source"], text=passage["text"])
                        for passage in passages
                        if references.intersection(passage["supports"])
                    ]
        elif needed_labels:
            payload["repair_evidence"] = [
                row for row in evidence_view(packet) if needed_labels.intersection(row["supports"])
            ]
        if historical(projection_version):
            payload["necessary_limits"] = list(packet.dossier.throughline_constraints)
        payload["decision"] = deepcopy(decision)
    attach_reference(payload, stage, craft_reference)
    validate_model_content(payload, materials.documents)
    return payload
