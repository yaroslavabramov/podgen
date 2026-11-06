"""Allowlisted cold input, independently sourced fact input, and complete attributed feedback."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .projection_versions import CURRENT, historical
from .projections import (
    actionable_feedback,
    assignment_view,
    evidence_view,
    indexed_draft,
    knowledge_view,
    validate_model_content,
)
from .story_views import editorial_frame, selected_labels


def reading_payload(
    stage, materials, *, script, readings=None, decision=None, approved_plan=None, projection_version=CURRENT
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
        payload["revision_guidance"] = [
            dict(
                origin=origin, **actionable_feedback(readings[origin], projection_version=projection_version)
            )
            for origin in ("reader", "fact")
        ]
        payload["support"] = knowledge_view(packet, labels=True, selected=selected)
        if needed_labels:
            payload["repair_evidence"] = [
                row for row in evidence_view(packet) if needed_labels.intersection(row["supports"])
            ]
        if historical(projection_version):
            payload["necessary_limits"] = list(packet.dossier.throughline_constraints)
        payload["decision"] = deepcopy(decision)
    validate_model_content(payload, materials.documents)
    return payload
