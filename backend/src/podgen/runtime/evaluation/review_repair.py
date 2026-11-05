"""One operator-authorized selection repair; immutable editorial judgments stay untouched."""

import json
from copy import deepcopy
from uuid import uuid5

from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
from podgen.writing.roles import EditorialFeedback, WriteOutput

from .compiler import indexed_script
from .contracts import RunFailure
from .validation import load_unique_json

# Frozen compatibility for manifests created before repair instructions were pinned.
LEGACY_REPAIR_INSTRUCTIONS_V1 = (
    "\n\nAnchor selection repair v1. This call selects exact source passages for the rejected anchors only. "
    "Keep the draft, sources, all editorial judgments and already valid anchors unchanged. "
    "Return AnchorChoices using only the published anchor_id and selector_id values. "
    "Choose selector_id=null when no offered passage supports the judgment. "
    "No factual corrections or new evidence are being supplied. "
    "Use the response schema for AnchorChoices on this dedicated call."
)


def _rejected_feedback(journal, original, request):
    if (
        original["stage"] != "review"
        or original["operation_kind"] != "role"
        or original["status"] != "validation_failed"
    ):
        raise RunFailure("review_anchor_repair_not_eligible")
    raw = journal.last_response(original)
    try:
        response = json.loads(raw)
        if (
            response["status"] != "completed"
            or response["id"] != original["provider_response_id"]
            or response["model"] != request["model"]
            or response["metadata"] != request["metadata"]
        ):
            raise ValueError()
        text = ""
        for item in response["output"]:
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item["role"] != "assistant":
                raise ValueError()
            for content in item["content"]:
                if content["type"] != "output_text":
                    raise ValueError()
                text += content["text"]
        load_unique_json(text)
        feedback = EditorialFeedback.model_validate_json(text)
        if feedback.reading_mode != "informed":
            raise ValueError()
        return feedback
    except (ValueError, TypeError, KeyError, ValidationError):
        raise RunFailure("review_anchor_repair_not_eligible") from None


def _plan(journal, original, draft):
    request = journal.request(original)
    feedback = _rejected_feedback(journal, original, request)
    script = WriteOutput.model_validate(draft)
    plan = AnchorRepairPlan.build(script, feedback)
    if not plan.broken:
        raise RunFailure("review_anchor_repair_not_eligible")
    if original["input_tokens"] is None:
        raise RunFailure("review_anchor_repair_usage_unknown")
    payload = json.loads(request["input"][0]["content"][0]["text"])
    if payload.get("draft") != indexed_script(draft):
        raise RunFailure("review_repair_draft_binding_mismatch")
    return plan, request


def prepare_anchor_repair(journal, original, *, draft, draft_ref, guard):
    guard()
    repairs = [
        row
        for row in journal.attempts(original["run_id"], "review")
        if row["operation_kind"] == "anchor_repair"
    ]
    if repairs:
        return repairs[-1]
    plan, request = _plan(journal, original, draft)
    body = deepcopy(request)
    invocation_id = uuid5(original["run_id"], "invocation:review:anchor-repair:1")
    body["metadata"].update(
        podgen_invocation_id=str(invocation_id),
        podgen_attempt_index=str(original["attempt_index"] + 1),
        podgen_parent_invocation_id=str(original["invocation_id"]),
    )
    manifest = journal.manifest(original["run_id"])
    pinned = manifest.get("anchor_repair_instructions")
    if "anchor_repair_instructions" not in manifest:
        pinned = dict(version="anchor-repair/1", body=LEGACY_REPAIR_INSTRUCTIONS_V1)
    if (
        not isinstance(pinned, dict)
        or set(pinned) != {"version", "body"}
        or not isinstance(pinned["version"], str)
        or not isinstance(pinned["body"], str)
        or not pinned["body"].strip()
    ):
        raise ContractError("invalid_pinned_anchor_repair_instructions")
    body["instructions"] += pinned["body"]
    body["text"]["format"] = dict(
        type="json_schema", name="anchor_choices", schema=AnchorChoices.model_json_schema(), strict=True
    )
    body["input"].append(
        dict(
            role="user",
            content=[
                dict(
                    type="input_text",
                    text=json.dumps(
                        dict(
                            contract="review-anchor-selection-v1",
                            rejected_feedback=plan.feedback.model_dump(mode="json"),
                            **plan.as_payload(),
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            ],
        )
    )
    context = deepcopy(
        journal.read(RuntimeRef(record_kind="tool_execution", record_id=original["request_record_id"]))[
            "context"
        ]
    )
    context.update(
        parent_request_ref=RuntimeRef(
            record_kind="tool_execution", record_id=original["request_record_id"]
        ).model_dump(mode="json"),
        draft_ref=draft_ref,
        repair_plan_hash=digest(plan.as_payload()),
        anchor_repair_instructions_version=pinned["version"],
        recovery=dict(
            kind="exact-anchor-selection-v1",
            maximum_repair_attempts=1,
            authorized_actor_id=str(journal.repo.actor_id),
        ),
    )
    invocation = journal.prepare(
        original["run_id"],
        "review",
        body,
        context,
        attempt_index=original["attempt_index"] + 1,
        attempt_kind="anchor_repair",
        operation_kind="anchor_repair",
        parent_invocation_id=original["invocation_id"],
    )
    journal.event(
        invocation,
        "anchor_repair_authorized",
        data=dict(
            parent_invocation_id=str(original["invocation_id"]),
            actor_id=str(journal.repo.actor_id),
            invalid_anchors=plan.as_payload()["invalid_anchors"],
            maximum_repair_attempts=1,
        ),
    )
    return invocation


def apply_anchor_repair(journal, invocation, raw, *, draft):
    attempts = journal.attempts(invocation["run_id"], "review")
    origin = next((row for row in attempts if row["attempt_kind"] == "anchor_repair"), None)
    original = next(
        (row for row in attempts if origin and row["invocation_id"] == origin["parent_invocation_id"]), None
    )
    if original is None or invocation["operation_kind"] != "anchor_repair":
        raise ContractError("review_repair_parent_mismatch")
    plan, _ = _plan(journal, original, draft)
    request_record = journal.read(
        RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
    )
    if request_record["context"]["repair_plan_hash"] != digest(plan.as_payload()):
        raise ContractError("anchor_repair_plan_changed")
    try:
        load_unique_json(raw)
    except ValueError:
        raise ContractError("invalid_anchor_choices_json") from None
    choices = AnchorChoices.model_validate_json(raw)
    repaired = plan.apply(choices)
    return repaired.model_dump(mode="json"), choices.model_dump(mode="json")
