"""Explicit, one-shot repair of addresses in an otherwise valid, saved planning review."""

import json
from copy import deepcopy
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, CodeProducer, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import one
from podgen.writing.plan_targets import PlanTargetChoices, PlanTargetRepair
from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput
from podgen.writing.prompts import load_plan_target_repair_instructions

from .contracts import RunFailure
from .rejected_output import rejected_output
from .validation import load_unique_json

REVIEW_STAGES = ("plan-review", "plan-review-revised")


def repair_plan(journal, original):
    if original["stage"] not in REVIEW_STAGES:
        raise RunFailure("plan_target_repair_not_eligible")
    review, request, response_ref = rejected_output(
        journal, original, PlanReviewOutput, code="plan_target_repair_not_eligible"
    )
    payload = load_unique_json(request["input"][0]["content"][0]["text"])
    candidate = PlanningOutput.model_validate(payload["candidate_plan"])
    context = journal.read(RuntimeRef(record_kind="tool_execution", record_id=original["request_record_id"]))[
        "context"
    ]
    ref = payload["candidate_plan_ref"]
    saved = journal.read(RuntimeRef.model_validate(ref))
    if (
        saved["run_id"] != str(original["run_id"])
        or saved["output"] != payload["candidate_plan"]
        or context["candidate_plan_ref"] != ref
        or context["candidate_plan_hash"] != digest(saved["output"])
        or payload["candidate_plan_hash"] != context["candidate_plan_hash"]
    ):
        raise ContractError("plan_target_candidate_binding_mismatch")
    try:
        plan = PlanTargetRepair.build(candidate, review, payload["author_materials"]["material_catalog"])
    except ValueError:
        raise RunFailure("plan_target_repair_not_eligible") from None
    if not plan.invalid:
        raise RunFailure("plan_target_repair_not_eligible")
    return plan, request, context, response_ref


def pin_repair_prompt(journal, run_id, invocation_id):
    operation = uuid5(invocation_id, "repair-prompt")
    with journal.repo.database.transaction() as tx:
        journal.authorize(tx)
        row = one(
            tx,
            "SELECT artifact_id,revision_id,kind FROM artifact_revisions WHERE project_id=:p AND operation_id=:o",
            p=journal.project,
            o=operation,
        )
    if row:
        prompt = journal.repo.get(journal.project, ArtifactRef.model_validate(dict(row)))
        return prompt.ref, prompt.payload["template"], prompt.producer.build_id
    pinned = journal.manifest(run_id).get("plan_target_repair_instructions")
    if pinned is None:
        instructions = load_plan_target_repair_instructions()
        body, version = instructions.body, instructions.version
    else:
        body, version = pinned["body"], pinned["version"]
    prompt = journal.repo.create(
        journal.project,
        "PromptDefinition",
        dict(
            role="plan-target-repair",
            template=body,
            placeholders=[],
            examples=[],
            compiler_version="plan-target-repair/1",
        ),
        producer=CodeProducer(
            kind="code", component="plan-target-repair", build_id=version, operation_id=operation
        ),
        operation_id=operation,
    )
    return prompt.ref, body, version


def authorize_plan_target_repair(journal, run_id, target_id, *, guard, allow_create=True):
    original = None
    rows = []
    for stage in REVIEW_STAGES:
        found = journal.attempts(run_id, stage)
        target = next((row for row in found if row["invocation_id"] == target_id), None)
        if target:
            original, rows = target, found
            break
    if original is None:
        raise RunFailure("plan_target_repair_not_eligible", run_id=run_id)
    plan, request, context, response_ref = repair_plan(journal, original)
    origins = [row for row in rows if row["attempt_kind"] == "plan_target_repair"]
    if origins:
        origin = origins[0]
        if origin["parent_invocation_id"] != target_id:
            raise RunFailure("plan_target_repair_already_used", run_id=run_id)
        return rows[-1]
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("plan_target_repair_not_current", run_id=run_id)
    guard()
    new_id = uuid5(target_id, "plan-target-repair:1")
    prompt_ref, suffix, version = pin_repair_prompt(journal, run_id, new_id)
    body = deepcopy(request)
    index = original["attempt_index"] + 1
    body["metadata"].update(
        podgen_invocation_id=str(new_id),
        podgen_attempt_index=str(index),
        podgen_parent_invocation_id=str(target_id),
    )
    body["instructions"] += "\n\n" + suffix
    body["text"]["format"] = dict(
        type="json_schema",
        name="plan_target_choices",
        schema=PlanTargetChoices.model_json_schema(),
        strict=True,
    )
    body["input"].append(
        dict(
            role="user",
            content=[
                dict(
                    type="input_text",
                    text=json.dumps(
                        dict(
                            contract="plan-target-repair-v1",
                            original_review=plan.review.model_dump(mode="json"),
                            **plan.as_payload(),
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            ],
        )
    )
    return journal.prepare(
        run_id,
        original["stage"],
        body,
        deepcopy(context),
        attempt_index=index,
        parent_invocation_id=target_id,
        attempt_kind="plan_target_repair",
        operation_kind="plan_target_repair",
        authorization=dict(
            kind="plan-target-repair-v1",
            actor_id=str(journal.repo.actor_id),
            source_response_ref=response_ref.model_dump(mode="json"),
            candidate_plan_ref=context["candidate_plan_ref"],
            candidate_plan_hash=context["candidate_plan_hash"],
            repair_prompt_ref=prompt_ref.model_dump(mode="json"),
            repair_instructions_version=version,
            repair_selection_hash=digest(plan.as_payload()),
            maximum_semantic_repairs=1,
        ),
    )


def apply_plan_target_repair(journal, invocation, raw, *, candidate, candidate_ref):
    rows = journal.attempts(invocation["run_id"], invocation["stage"])
    origin = next(row for row in rows if row["attempt_kind"] == "plan_target_repair")
    original = next(row for row in rows if row["invocation_id"] == origin["parent_invocation_id"])
    plan, _, context, _ = repair_plan(journal, original)
    request = journal.read(RuntimeRef(record_kind="tool_execution", record_id=origin["request_record_id"]))
    if (
        candidate != plan.candidate.model_dump(mode="json")
        or candidate_ref != context["candidate_plan_ref"]
        or request["authorization"]["repair_selection_hash"] != digest(plan.as_payload())
    ):
        raise ContractError("plan_target_candidate_binding_mismatch")
    load_unique_json(raw)
    choices = PlanTargetChoices.model_validate_json(raw)
    return plan.apply(choices), choices.model_dump(mode="json")
