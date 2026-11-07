"""Bind a continuation to the exact last rejected candidate, review and frozen inputs."""

from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput

from ..compiler import materials_hash
from ..contracts import WritingRunCommand

KEY = "planning_continuation"
CONTRACT = "planning-continuation/1"


def last_pair(journal, run_id):
    """Find the latest completed candidate and a completed review of that exact version."""
    stages = journal.manifest(run_id)["stages"]
    candidates = [journal.invocation(run_id, s) for s in stages if s in ("plan", "plan-revise")]
    selected = next((row for row in reversed(candidates) if row and row["status"] == "completed"), None)
    if selected is None:
        raise ContractError("planning_continuation_candidate_missing")
    candidate_ref = RuntimeRef(record_kind="tool_execution", record_id=selected["output_record_id"])
    candidate = journal.output(selected)
    for stage in reversed(stages):
        if stage not in ("plan-review", "plan-review-revised"):
            continue
        row = journal.invocation(run_id, stage)
        if row and row["status"] == "completed":
            review = journal.output(row)
            if review.get("candidate_plan_ref") == candidate_ref.model_dump(mode="json"):
                if (
                    candidate.get("run_id") != str(run_id)
                    or review.get("run_id") != str(run_id)
                    or review.get("candidate_plan_hash") != digest(candidate["output"])
                ):
                    raise ContractError("planning_continuation_review_binding_mismatch")
                return (
                    candidate_ref,
                    candidate,
                    RuntimeRef(record_kind="tool_execution", record_id=row["output_record_id"]),
                    review,
                )
    raise ContractError("planning_continuation_review_missing")


def _eligible(journal, parent_id):
    row = journal.run(parent_id)
    if row is None or row["failure_code"] != "planning_revision_limit_exhausted":
        raise ContractError("planning_continuation_requires_exhausted_run")
    manifest = journal.manifest(parent_id)
    command = WritingRunCommand.model_validate(manifest["command"])
    if (
        manifest.get("input_contract") != "editorial-role-packets/1"
        or command.input_contract != "editorial-role-packets/1"
        or command.plan_mode != "own-plan"
    ):
        raise ContractError("planning_continuation_requires_editorial_own_plan")
    if any(
        journal.invocation(parent_id, stage) is not None
        for stage in manifest["stages"]
        if not stage.startswith("plan")
    ):
        raise ContractError("planning_continuation_source_already_writing")
    return manifest, command


def _lineage(manifest, parent_id, limit):
    prior = manifest.get(KEY)
    if prior is None:
        if limit is None or limit < 1:
            raise ContractError("planning_continuation_limit_required")
        return parent_id, 1, limit
    if prior.get("contract") != CONTRACT:
        raise ContractError("unknown_planning_continuation_contract")
    inherited = prior["max_additional_revisions"]
    if limit is not None and limit != inherited:
        raise ContractError("planning_continuation_limit_changed")
    depth = prior["depth"] + 1
    if depth > inherited:
        raise ContractError("planning_continuation_limit_exhausted")
    return UUID(prior["root_run_id"]), depth, inherited


def continuation_id(parent_id):
    return uuid5(parent_id, CONTRACT)


def _command(source, parent_id, run_id, ceiling):
    values = source.model_dump(mode="json")
    values.update(
        run_id=str(run_id),
        planning_parent_run_id=str(parent_id),
        planning_continuation_limit=ceiling,
        max_plan_revisions=1,
    )
    return WritingRunCommand.model_validate(values)


def build_command(journal, parent_id, run_id=None, *, limit=None):
    expected_id = continuation_id(parent_id)
    if run_id is not None and run_id != expected_id:
        raise ContractError("planning_continuation_run_id_mismatch")
    run_id = expected_id
    manifest, source = _eligible(journal, parent_id)
    _, _, ceiling = _lineage(manifest, parent_id, limit)
    return _command(source, parent_id, run_id, ceiling)


def source_binding(journal, command, materials):
    parent = command.planning_parent_run_id
    if command.run_id != continuation_id(parent):
        raise ContractError("planning_continuation_run_id_mismatch")
    manifest, source = _eligible(journal, parent)
    root, depth, limit = _lineage(manifest, parent, command.planning_continuation_limit)
    expected = _command(source, parent, command.run_id, limit)
    if command != expected:
        raise ContractError("planning_continuation_command_binding_mismatch")
    if manifest["materials_hash"] != materials_hash(materials.as_payload()):
        raise ContractError("planning_continuation_material_binding_mismatch")
    candidate_ref, candidate, review_ref, review = last_pair(journal, parent)
    proposal = PlanningOutput.model_validate(candidate["output"])
    assessment = PlanReviewOutput.model_validate(review["output"])
    proposal.validate_against(
        material_locators=materials.material_catalog, speaker_ids=materials.speaker_labels
    )
    assessment.validate_against(proposal, material_locators=materials.material_catalog)
    if (
        proposal.status != "ready"
        or assessment.decision != "revise"
        or any(item.blocking for item in assessment.research_requests)
    ):
        raise ContractError("planning_continuation_requires_revisable_candidate")
    binding = dict(
        contract=CONTRACT,
        parent_run_id=str(parent),
        root_run_id=str(root),
        depth=depth,
        max_additional_revisions=limit,
        parent_manifest_hash=digest(manifest),
        candidate_plan_ref=candidate_ref.model_dump(mode="json"),
        candidate_plan_hash=digest(proposal.model_dump(mode="json")),
        plan_review_ref=review_ref.model_dump(mode="json"),
        plan_review_hash=digest(assessment.model_dump(mode="json")),
        materials_hash=manifest["materials_hash"],
    )
    return binding, manifest, candidate, review


def verify(journal, command, manifest, materials):
    binding, _, candidate, review = source_binding(journal, command, materials)
    if manifest.get(KEY) != binding:
        raise ContractError("planning_continuation_binding_changed")
    return candidate, review
