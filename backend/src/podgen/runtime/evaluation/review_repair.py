"""One operator-authorized selection repair; immutable editorial judgments stay untouched."""

import json
from copy import deepcopy
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
from podgen.writing.revision import verification
from podgen.writing.roles import WriteOutput

from .compiler import indexed_script
from .contracts import RunFailure
from .reading_workflow import base_stage, is_reading
from .rejected_output import rejected_output
from .validation import load_unique_json
from .workflow import output_model

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
    if not is_reading(original["stage"]):
        raise RunFailure("review_anchor_repair_not_eligible")
    manifest = journal.manifest(original["run_id"])
    feedback, _, _ = rejected_output(
        journal,
        original,
        output_model(
            original["stage"],
            reader_schema_version=manifest.get("reader_review_schema_version"),
            revision_verification_contract=manifest.get(verification.KEY),
            fact_schema_version=manifest.get("fact_review_schema_version"),
        ),
        code="review_anchor_repair_not_eligible",
    )
    recheck = verification.applies(original["stage"], manifest.get(verification.KEY))
    expected_mode = (
        "script_only" if base_stage(original["stage"]) == "reader-review" and not recheck else "informed"
    )
    if feedback.reading_mode != expected_mode:
        raise RunFailure("review_anchor_repair_not_eligible")
    return feedback


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
    if payload.get("draft", payload.get("script")) != indexed_script(draft):
        raise RunFailure("review_repair_draft_binding_mismatch")
    return plan, request


def prepare_anchor_repair(journal, original, *, draft, draft_ref, guard):
    guard()
    repairs = [
        row
        for row in journal.attempts(original["run_id"], original["stage"])
        if row["operation_kind"] == "anchor_repair"
    ]
    if repairs:
        return repairs[-1]
    plan, request = _plan(journal, original, draft)
    body = deepcopy(request)
    invocation_id = uuid5(original["run_id"], "invocation:" + original["stage"] + ":anchor-repair:1")
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
    if manifest.get("input_contract") == "editorial-role-packets/1":
        from .repair_projection import anchor_payload, replace_input

        replace_input(body, pinned["body"], anchor_payload(plan))
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
        original["stage"],
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
    attempts = journal.attempts(invocation["run_id"], invocation["stage"])
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
    if request_record["context"].get("role_input_contract") == "editorial-role-packets/1":
        repair_request = journal.request(invocation)
        offered = load_unique_json(repair_request["input"][0]["content"][0]["text"])
        allowed = {row["anchor_id"]: set(row["selector_ids"]) for row in offered["invalid_anchors"]}
        for choice in choices.choices:
            if choice.selector_id is not None and choice.selector_id not in allowed.get(
                choice.anchor_id, set()
            ):
                raise ContractError("unoffered_anchor_selector", choice.anchor_id)
    repaired = plan.apply(choices)
    return repaired.model_dump(mode="json"), choices.model_dump(mode="json")
