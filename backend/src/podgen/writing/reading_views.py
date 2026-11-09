"""Allowlisted cold input, independently sourced fact input, and complete attributed feedback."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from . import author_plan
from .craft_reference import attach_reference
from .duration_views import duration_context
from .projection_versions import (
    CURRENT,
    brief_reading_audience,
    creative_materials,
    historical,
    script_handoff,
)
from .projections import (
    assignment_view,
    attach_author_support,
    evidence_view,
    indexed_draft,
    knowledge_view,
    validate_model_content,
)
from .prompt_cache import validate as validate_cache_layout
from .revision import assignment as edit_assignment
from .revision import feedback as legacy_feedback
from .revision.context import REQUIRED_VERSION
from .revision.context import VERSION as EDIT_ASSIGNMENT
from .scene_plan import claim_scopes
from .source_passages import validate_strategy as validate_excerpt_strategy
from .story_views import editorial_frame, selected_labels


def reading_audience(assignment, manifest=None, *, projection_version=CURRENT):
    if manifest is not None and "reading_audience" in manifest:
        return manifest["reading_audience"]
    if manifest is None and brief_reading_audience(projection_version):
        return assignment.listener_profile or assignment.audience
    # Older projections and runs without an audience pin used this exact default.
    return assignment.listener_profile or "General listeners; no prior topic knowledge is assumed."


def reading_payload(
    stage,
    materials,
    *,
    script,
    audience=None,
    readings=None,
    decision=None,
    approved_plan=None,
    projection_version=CURRENT,
    craft_reference=None,
    revision_operation=None,
    composition=None,
    plan_selection_contract=None,
    revision_context_contract=None,
    author_plan_handoff=None,
    author_source_excerpts=None,
    prompt_cache_layout=None,
):
    validate_cache_layout(prompt_cache_layout, projection_version)
    validate_excerpt_strategy(author_source_excerpts, projection_version)
    packet = materials.packet
    payload = dict(
        script=indexed_draft(script),
        audience=audience or reading_audience(packet.assignment, projection_version=projection_version),
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
        selected_plan = approved_plan or (
            materials.fixed.plan.model_dump(mode="json") if materials.fixed else None
        )
        scoped_claims = claim_scopes(selected_plan)
        if scoped_claims:
            payload["factual_remit"]["claims_to_check"] = scoped_claims
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
            payload["editorial_frame"] = (
                author_plan.story(plan)
                if author_plan_handoff
                else editorial_frame(
                    plan,
                    projection_version=projection_version,
                    plan_selection_contract=plan_selection_contract,
                )
            )
            if "revision_scope" in readings["reader"]:
                payload["editorial_frame"]["authority"] = "revisable_authorial_decisions"
            if prompt_cache_layout is not None:
                payload["accepted_plan"] = deepcopy(payload["editorial_frame"])
                if composition is None:
                    payload["editorial_frame"] = {
                        key: deepcopy(payload["accepted_plan"][key])
                        for key in (
                            "authority",
                            "main_question",
                            "listener_promise",
                            "route_summary",
                            "unresolved_questions",
                        )
                        if key in payload["accepted_plan"]
                    }
        if composition is not None:
            if author_plan_handoff and plan and prompt_cache_layout is None:
                payload["accepted_plan"] = payload["editorial_frame"]
            questions = deepcopy(payload.get("editorial_frame", {}).get("unresolved_questions", []))
            payload["editorial_frame"] = dict(
                authority="revisable_authorial_decisions",
                unresolved_questions=questions,
                **{
                    key: deepcopy(value)
                    for key, value in composition.items()
                    if key != "unresolved_questions"
                },
            )
            if "unresolved_questions" in composition:
                payload["editorial_frame"]["unresolved_questions"] = deepcopy(
                    composition["unresolved_questions"]
                )
        if revision_operation is not None:
            payload["revision_operation"] = deepcopy(revision_operation)
        compact = revision_context_contract == REQUIRED_VERSION
        if not compact:
            payload["revision_guidance"] = legacy_feedback.guidance(
                readings, projection_version=projection_version, addressed=revision_operation is not None
            )
        if creative_materials(projection_version):
            attach_author_support(
                payload,
                packet,
                plan,
                draft=False,
                author_plan_handoff=author_plan_handoff,
                author_source_excerpts=author_source_excerpts,
                selected=selected,
                projection_version=projection_version,
                plan_selection_contract=plan_selection_contract,
            )
        else:
            payload["support"] = knowledge_view(
                packet, labels=packet.contract != "editorial-input/2", selected=selected
            )
        if compact:
            payload["revision_tasks"] = edit_assignment.tasks(readings, payload["support"])
        else:
            legacy_feedback.attach_evidence(payload, packet, needed_labels)
        if historical(projection_version):
            payload["necessary_limits"] = list(packet.dossier.throughline_constraints)
        if not compact:
            payload["decision"] = deepcopy(decision)
    if stage == "revise" and revision_context_contract == EDIT_ASSIGNMENT:
        # The editor's actionable and positive observations already address the supplied script.
        for review in payload["revision_guidance"]:
            review.pop("listener_understanding", None)
    elif not (stage == "revise" and revision_context_contract == REQUIRED_VERSION):
        attach_reference(payload, stage, craft_reference)
    validate_model_content(payload, materials.documents)
    return payload
