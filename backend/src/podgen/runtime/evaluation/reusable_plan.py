"""Read a journal clearance as an immutable fixed plan, preserving its real source and audience."""

from typing import Literal
from uuid import UUID

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import Contract, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.repository import Repository
from podgen.writing.editorial_contract import EditorialPlan
from podgen.writing.planning_versions import planning_snapshot

from .journal import EvaluationJournal
from .planning import clearance_ref, verify_clearance


class ApprovedEditorialPlan(Contract):
    contract: Literal["approved-editorial-plan/1"] = "approved-editorial-plan/1"
    source_run_id: UUID
    clearance_ref: RuntimeRef
    candidate_ref: RuntimeRef
    candidate_hash: str
    # Use exactly the same supported plan variants as an admitted fixed plan.
    plan: EditorialPlan


def export_plan_ref(journal, run_id):
    manifest = journal.manifest(run_id)
    if (
        manifest.get("input_contract") != "editorial-role-packets/1"
        or manifest.get("plan_mode") != "own-plan"
    ):
        raise ContractError("accepted_plan_requires_own_plan")
    verify_clearance(journal, run_id)
    return clearance_ref(run_id)


def load_approved_plan(repo, project, shared_ref, packet, clearance):
    # Only evaluation-capable actors may reuse evaluation-only clearances.
    scoped = Repository(repo.database, repo.objects, repo.actor_id, purpose="evaluation")
    journal = EvaluationJournal(scoped, project)
    with scoped.database.read_transaction() as tx:
        journal.authorize(tx)
    record = journal.read(clearance)
    if record.get("contract") != "writing-planning-clearance-v1":
        raise ContractError("expected_planning_clearance")
    run_id = UUID(record["run_id"])
    if export_plan_ref(journal, run_id) != clearance:
        raise ContractError("accepted_plan_clearance_mismatch")
    manifest = journal.manifest(run_id)
    if manifest["author_ref"] != shared_ref.model_dump(mode="json"):
        raise ContractError("accepted_plan_shared_input_mismatch")
    material_ref = RuntimeRef.model_validate(record["candidate_plan_ref"])
    saved = journal.read(material_ref)["output"]
    plan = planning_snapshot(saved)
    from podgen.writing.card_catalog import catalog

    plan.validate_against(
        material_locators=catalog(packet), speaker_ids=[s.label for s in packet.assignment.speakers]
    )
    if digest(plan.model_dump(mode="json")) != record["candidate_plan_hash"]:
        raise ContractError("accepted_plan_hash_mismatch")
    return ApprovedEditorialPlan(
        source_run_id=run_id,
        clearance_ref=clearance,
        candidate_ref=material_ref,
        candidate_hash=record["candidate_plan_hash"],
        plan=plan,
    )
