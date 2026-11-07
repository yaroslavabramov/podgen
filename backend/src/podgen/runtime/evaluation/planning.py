"""Bounded own-plan evaluation. A model decision is not canonical production acceptance."""

from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from .contracts import RunFailure
from .planning_stage import execute_planning_stage


def has_blocking_requests(output):
    return any(item["blocking"] for item in output.get("research_requests", []))


def clearance_ref(run_id):
    return RuntimeRef(record_kind="tool_execution", record_id=uuid5(run_id, "planning-clearance"))


def execute_planning(
    *, journal, gateway, generation, command, manifest, materials, outputs, records, totals, guard
):
    def call(stage, **bindings):
        guard()
        result = execute_planning_stage(
            stage,
            journal=journal,
            gateway=gateway,
            generation=generation,
            command=command,
            manifest=manifest,
            materials=materials,
            totals=totals,
            guard=guard,
            **bindings,
        )
        outputs[stage] = result.output
        records[stage] = result.ref.model_dump(mode="json")
        totals["input"] += result.input_tokens
        totals["output"] += result.output_tokens_or_reservation
        return result.output, records[stage]

    if command.planning_parent_run_id is not None:
        from .planning_continuation.source import verify

        seed, assessment = verify(journal, command, manifest, materials)
        candidate, review = seed["output"], assessment["output"]
        binding = manifest["planning_continuation"]
        candidate_ref, review_ref = binding["candidate_plan_ref"], binding["plan_review_ref"]
    else:
        candidate, candidate_ref = call("plan")
        if has_blocking_requests(candidate):
            raise RunFailure("planning_needs_research", run_id=command.run_id)
        review, review_ref = call("plan-review", candidate=candidate, candidate_ref=candidate_ref)
    if review["decision"] == "revise" and not has_blocking_requests(review) and command.max_plan_revisions:
        candidate, candidate_ref = call(
            "plan-revise",
            candidate=candidate,
            candidate_ref=candidate_ref,
            review=review,
            review_ref=review_ref,
        )
        if has_blocking_requests(candidate):
            raise RunFailure("planning_needs_research", run_id=command.run_id)
        review, review_ref = call("plan-review-revised", candidate=candidate, candidate_ref=candidate_ref)
    if has_blocking_requests(review) or review["decision"] == "needs_research":
        raise RunFailure("planning_needs_research", run_id=command.run_id)
    if review["decision"] != "accept":
        raise RunFailure("planning_revision_limit_exhausted", run_id=command.run_id)
    proof = dict(
        contract="writing-planning-clearance-v1",
        run_id=str(command.run_id),
        candidate_plan_ref=candidate_ref,
        candidate_plan_hash=digest(candidate),
        plan_review_ref=review_ref,
        executed_stages=list(records),
        decision="accept",
        semantic_acceptance=False,
        production_ready=False,
    )
    target_repairs = [
        row
        for stage in records
        for row in journal.attempts(command.run_id, stage)
        if row["attempt_kind"] == "plan_target_repair"
    ]
    if target_repairs:
        proof["additional_conditions"] = [
            dict(kind="plan-target-repair-v1", invocation_id=str(row["invocation_id"]), stage=row["stage"])
            for row in target_repairs
        ]
    journal.record(proof, record_id=clearance_ref(command.run_id).record_id)
    return dict(
        plan_mode="own-plan",
        accepted_plan=candidate,
        accepted_plan_ref=candidate_ref,
        accepted_plan_hash=digest(candidate),
        plan_review=review,
        plan_review_ref=review_ref,
        planning_clearance_ref=clearance_ref(command.run_id).model_dump(mode="json"),
    )


def verify_clearance(journal, run_id):
    proof = journal.read(clearance_ref(run_id))
    candidate = journal.read(RuntimeRef.model_validate(proof["candidate_plan_ref"]))
    review = journal.read(RuntimeRef.model_validate(proof["plan_review_ref"]))
    if (
        proof["run_id"] != str(run_id)
        or proof["decision"] != "accept"
        or candidate["run_id"] != str(run_id)
        or review["run_id"] != str(run_id)
        or review["candidate_plan_ref"] != proof["candidate_plan_ref"]
        or review["candidate_plan_hash"] != proof["candidate_plan_hash"]
        or digest(candidate["output"]) != proof["candidate_plan_hash"]
        or review["output"]["decision"] != "accept"
        or has_blocking_requests(candidate["output"])
        or has_blocking_requests(review["output"])
    ):
        raise ContractError("invalid_planning_clearance")
    return proof
