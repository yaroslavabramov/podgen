"""Role-specific semantic views. No raw-corpus fallback, truncation, or prose cleanup."""

import re
from copy import deepcopy
from pathlib import PurePosixPath

from podgen.domain.errors import ContractError

from .card_catalog import card_id
from .creative_material import creative_card_view, creative_dossier_view
from .plan_views import candidate_view
from .projection_versions import CURRENT, V1, creative_materials, historical, validate_version
from .story_views import (
    editorial_frame,
    narrative_decisions,
    selected_labels,
    story_intent,
)

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
    validate_model_content(
        [card.story_facts for card in packet.dossier.knowledge_cards if card.story_facts is not None],
        documents,
    )


def assignment_view(packet):
    assignment = packet.assignment.model_dump(mode="json")
    assignment.pop("key")
    for field in ("excluded_topics", "style_guidance"):
        if not assignment[field]:
            assignment.pop(field)
    return assignment


def knowledge_view(packet, *, labels, selected=None):
    documents = {doc.key: doc for doc in packet.documents}
    cards = []
    for card in packet.dossier.knowledge_cards:
        if selected is not None and card_id(packet, card) not in selected:
            continue
        value = dict(
            title=card.title,
            claim=card.claim,
            context=card.context,
            limitations=list(card.limitations),
            sources=list(dict.fromkeys(documents[e.document].title for e in card.excerpts)),
        )
        if packet.contract == "editorial-input/2":
            value["label"] = card.label
            if labels:
                value["card_id"] = card.card_id
        elif labels:
            value["label"] = card.label
        cards.append(value)
    return cards


def creative_knowledge_view(packet, *, labels, selected=None):
    documents = {doc.key: doc for doc in packet.documents}
    result = []
    for card in packet.dossier.knowledge_cards:
        identity = card_id(packet, card)
        if selected is not None and identity not in selected:
            continue
        value = creative_card_view(card, sources=[documents[quote.document].title for quote in card.excerpts])
        if labels:
            value["card_id" if packet.contract == "editorial-input/2" else "label"] = identity
        result.append(value)
    return result


def actionable_feedback(review, *, planning=False, projection_version=CURRENT):
    result = {
        key: deepcopy(review[key])
        for key in ("findings", "research_requests", "important_limits", "duration_note")
        if key in review and (key != "important_limits" or historical(projection_version))
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
    stage,
    materials,
    *,
    projection_version=CURRENT,
    candidate=None,
    plan_review=None,
    approved_plan=None,
    draft=None,
    feedback=None,
):
    validate_version(projection_version)
    packet = materials.packet
    payload = dict(output_language=materials.output_language)
    if projection_version != V1 and stage in ("review", "revise"):
        payload["draft"] = indexed_draft(draft)
    payload["assignment"] = assignment_view(packet)
    if stage.startswith("plan"):
        payload["dossier"] = dict(
            summary=packet.dossier.summary,
            throughline_constraints=list(packet.dossier.throughline_constraints),
            threads=[thread.model_dump(mode="json") for thread in packet.dossier.threads],
            knowledge_cards=knowledge_view(packet, labels=True),
        )
        if creative_materials(projection_version) and stage in ("plan", "plan-revise"):
            payload["dossier"] = creative_dossier_view(creative_knowledge_view(packet, labels=True))
        payload["material_catalog"] = list(materials.material_catalog)
        if stage.startswith("plan-review"):
            payload["evidence"] = evidence_view(packet)
        if candidate is not None:
            payload["candidate_plan"] = candidate_view(candidate, projection_version=projection_version)
        if stage.startswith("plan-review"):
            from .plan_targets import allowed_review_targets

            payload["review_target_root"] = "The proposal itself, without a surrounding request wrapper."
            payload["allowed_review_targets"] = allowed_review_targets(payload["candidate_plan"])
        if stage == "plan-revise":
            if plan_review is None:
                raise ContractError("plan_review_required")
            payload["revision_guidance"] = actionable_feedback(
                plan_review, planning=True, projection_version=projection_version
            )
    else:
        plan = (
            approved_plan
            if approved_plan is not None
            else materials.fixed.plan.model_dump(mode="json")
            if materials.fixed
            else None
        )
        if stage == "draft":
            payload["story"] = narrative_decisions(plan, projection_version=projection_version)
            payload["support"] = (
                creative_knowledge_view if creative_materials(projection_version) else knowledge_view
            )(packet, labels=False, selected=selected_labels(plan, packet))
        elif stage == "review":
            if projection_version == V1:
                payload["story_intent"] = story_intent(plan)
                payload["draft"] = indexed_draft(draft)
            else:
                payload["editorial_frame"] = editorial_frame(plan, projection_version=projection_version)
            payload["support"] = knowledge_view(packet, labels=True)
            payload["evidence"] = evidence_view(packet)
        elif stage == "revise":
            if projection_version == V1:
                payload["story_intent"] = story_intent(plan)
                payload["draft"] = indexed_draft(draft)
            else:
                payload["editorial_frame"] = editorial_frame(plan, projection_version=projection_version)
            if feedback is None:
                raise ContractError("feedback_required")
            payload["revision_guidance"] = actionable_feedback(
                feedback, projection_version=projection_version
            )
            payload["support"] = (
                creative_knowledge_view if creative_materials(projection_version) else knowledge_view
            )(packet, labels=False, selected=selected_labels(plan, packet))
        else:
            raise ContractError("unknown_role_projection")
        if historical(projection_version):
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
            key_id = card_id(packet, card)
            if key_id not in passages[key]["supports"]:
                passages[key]["supports"].append(key_id)
    return list(passages.values())
