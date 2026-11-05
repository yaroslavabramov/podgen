"""Role-specific semantic views. No raw-corpus fallback, truncation, or prose cleanup."""

import re
from copy import deepcopy
from pathlib import PurePosixPath

from podgen.domain.errors import ContractError

CONTRACT = "editorial-role-packets/1"
VISIBLE_BYTES_LIMIT = 128 * 1024
SINGLE_INPUT_TOKEN_LIMIT = 32768
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def validate_model_content(value, documents=(), path=""):
    forbidden = set()
    for doc in documents:
        forbidden.update(
            (doc.path, doc.sha256, str(getattr(doc.ref, "record_id", getattr(doc.ref, "revision_id", ""))))
        )
        forbidden.add(PurePosixPath(doc.path).name)

    def walk(item, at):
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, at + "/" + key)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, at + "/" + str(index))
        elif isinstance(item, str):
            if (
                UUID.search(item)
                or re.search(r"\b[0-9a-fA-F]{64}\b", item)
                or any(token and token in item for token in forbidden)
            ):
                raise ContractError("technical_identity_in_role_input", at)

    walk(value, path)


def validate_editorial_content(packet, documents):
    validate_model_content(assignment_view(packet), documents)
    validate_model_content(knowledge_view(packet, labels=True), documents)


def assignment_view(packet):
    assignment = packet.assignment.model_dump(mode="json")
    assignment.pop("key")
    return assignment


def knowledge_view(packet, *, labels, selected=None):
    documents = {doc.key: doc for doc in packet.documents}
    cards = []
    for card in packet.dossier.knowledge_cards:
        if selected is not None and card.label not in selected:
            continue
        value = dict(
            title=card.title,
            claim=card.claim,
            context=card.context,
            limitations=list(card.limitations),
            sources=list(dict.fromkeys(documents[e.document].title for e in card.excerpts)),
        )
        if labels:
            value["label"] = card.label
        cards.append(value)
    return cards


def selected_labels(plan, packet):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    body = plan["plan"]
    refs = [
        *body["opening"]["material_refs"],
        *(ref for act in body["acts"] for ref in act["material_refs"]),
        *(ref for question in plan["research_requests"] for ref in question["material_refs"]),
    ]
    if set(refs) - {card.label for card in packet.dossier.knowledge_cards}:
        raise ContractError("unknown_editorial_support_label")
    return set(refs)


def narrative_decisions(plan):
    source = plan["plan"]
    if source is None:
        raise ContractError("writing_plan_required")
    # Plot decisions only. Local plan anchors and source-selection labels are not writing instructions.
    result = dict(ending=deepcopy(source["ending"]), duration_strategy=deepcopy(source["duration_strategy"]))
    result["ending"].pop("promises_to_close")
    result["opening"] = {key: deepcopy(source["opening"][key]) for key in ("approach", "question_to_open")}
    act_fields = (
        "title",
        "listener_question",
        "intended_change_in_understanding",
        "prerequisite_concepts",
        "limitations_to_voice",
        "transitions",
        "estimated_seconds_range",
        "priority",
    )
    result["acts"] = [{key: deepcopy(act[key]) for key in act_fields} for act in source["acts"]]
    result["host_functions"] = [
        {("speaker" if key == "speaker_id" else key): deepcopy(value) for key, value in host.items()}
        for host in source["host_functions"]
    ]
    result["limitations"] = deepcopy(plan["limitations"])
    result["unresolved_questions"] = [
        dict(question=q["question"], why_needed=q["why_needed"]) for q in plan["research_requests"]
    ]
    return result


def actionable_feedback(review, *, planning=False):
    result = {
        key: deepcopy(review[key])
        for key in ("findings", "research_requests", "important_limits", "duration_note")
    }
    result["decision" if planning else "recommendation"] = review[
        "decision" if planning else "recommendation"
    ]
    if planning:
        result["decision_reason"] = review["decision_reason"]
        result["retain_sections"] = [dict(targets=deepcopy(row["targets"])) for row in review["strengths"]]
    if not planning:
        result["listener_understanding"] = review["listener_answer"]
        result["retain_passages"] = [dict(anchors=deepcopy(s["anchors"])) for s in review["strengths"]]
    return result


def indexed_draft(draft):
    if draft is None:
        raise ContractError("draft_required")
    return dict(utterances=[dict(index=i, **deepcopy(row)) for i, row in enumerate(draft["utterances"])])


def role_payload(
    stage, materials, *, candidate=None, plan_review=None, approved_plan=None, draft=None, feedback=None
):
    packet = materials.packet
    payload = dict(output_language=materials.output_language, assignment=assignment_view(packet))
    if stage.startswith("plan"):
        payload["dossier"] = dict(
            summary=packet.dossier.summary,
            throughline_constraints=list(packet.dossier.throughline_constraints),
            threads=[thread.model_dump(mode="json") for thread in packet.dossier.threads],
            knowledge_cards=knowledge_view(packet, labels=True),
        )
        payload["material_catalog"] = list(materials.material_catalog)
        if stage.startswith("plan-review"):
            payload["evidence"] = evidence_view(packet)
        if candidate is not None:
            payload["candidate_plan"] = deepcopy(candidate)
        if stage.startswith("plan-review"):
            from .plan_targets import allowed_review_targets

            payload["review_target_root"] = "The proposal itself, without a surrounding request wrapper."
            payload["allowed_review_targets"] = allowed_review_targets(candidate)
        if stage == "plan-revise":
            if plan_review is None:
                raise ContractError("plan_review_required")
            payload["revision_guidance"] = actionable_feedback(plan_review, planning=True)
    else:
        plan = (
            approved_plan
            if approved_plan is not None
            else materials.fixed.plan.model_dump(mode="json")
            if materials.fixed
            else None
        )
        if stage == "draft":
            payload["story"] = narrative_decisions(plan)
            payload["support"] = knowledge_view(packet, labels=False, selected=selected_labels(plan, packet))
        elif stage == "review":
            payload["story_intent"] = story_intent(plan)
            payload["draft"] = indexed_draft(draft)
            payload["support"] = knowledge_view(packet, labels=True)
            payload["evidence"] = evidence_view(packet)
        elif stage == "revise":
            payload["story_intent"] = story_intent(plan)
            payload["draft"] = indexed_draft(draft)
            if feedback is None:
                raise ContractError("feedback_required")
            payload["revision_guidance"] = actionable_feedback(feedback)
            payload["support"] = knowledge_view(packet, labels=False, selected=selected_labels(plan, packet))
        else:
            raise ContractError("unknown_role_projection")
        payload["necessary_limits"] = list(packet.dossier.throughline_constraints)
    validate_model_content(payload, materials.documents)
    return payload


def evidence_view(packet):
    documents = {d.key: d for d in packet.documents}
    passages = {}
    for card in packet.dossier.knowledge_cards:
        for excerpt in card.excerpts:
            key = (excerpt.document, excerpt.start, excerpt.end)
            if key not in passages:
                passages[key] = dict(source=documents[excerpt.document].title, text=excerpt.text, supports=[])
            if card.label not in passages[key]["supports"]:
                passages[key]["supports"].append(card.label)
    return list(passages.values())


def story_intent(plan):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    body = plan["plan"]
    return dict(
        answer=body["ending"]["answer_to_deliver"],
        qualifications=deepcopy(body["ending"]["qualifications"]),
        steps=[
            dict(
                question=act["listener_question"],
                change=act["intended_change_in_understanding"],
                transition=deepcopy(act["transitions"]),
            )
            for act in body["acts"]
        ],
    )
