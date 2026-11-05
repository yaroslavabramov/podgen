"""An unambiguous keep reuses the exact saved draft, with no synthetic model invocation."""

from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one

from .contracts import RunFailure


def final_ref(run_id):
    return RuntimeRef(record_kind="tool_execution", record_id=uuid5(run_id, "finalization"))


def review_action(feedback):
    decision = feedback["recommendation"]
    research = feedback["research_requests"]
    required = any(finding["priority"] in ("blocking", "important") for finding in feedback["findings"])
    if decision == "keep":
        if research or required:
            raise RunFailure("contradictory_editorial_decision")
        return "keep"
    if decision == "needs_material" or research:
        raise RunFailure("editorial_needs_material")
    if decision != "revise" or not feedback["findings"]:
        raise RunFailure("contradictory_editorial_decision")
    return "revise"


def finalize_keep(journal, command, outputs, records):
    record = dict(
        contract="writing-finalization-v1",
        run_id=str(command.run_id),
        origin="unchanged_draft",
        model_call=False,
        source_script_ref=records["draft"],
        review_ref=records["review"],
        output=outputs["draft"],
        semantic_acceptance=False,
        production_ready=False,
    )
    ref = journal.record(record, record_id=final_ref(command.run_id).record_id)
    return ref, record


def read_finalization(journal, run_id):
    ref = final_ref(run_id)
    with journal.repo.database.read_transaction() as tx:
        journal.authorize(tx)
        exists = one(
            tx,
            "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=:r",
            p=journal.project,
            r=ref.record_id,
        )
    if not exists:
        return None
    record = journal.read(ref)
    if (
        record.get("contract") != "writing-finalization-v1"
        or record.get("run_id") != str(run_id)
        or record.get("origin") != "unchanged_draft"
        or record.get("model_call") is not False
    ):
        raise Conflict("invalid_finalization_binding")
    if journal.invocation(run_id, "revise") is not None:
        raise Conflict("finalization_with_revision_invocation")
    draft_invocation = journal.invocation(run_id, "draft")
    review_invocation = journal.invocation(run_id, "review")
    if (
        not draft_invocation
        or not review_invocation
        or draft_invocation["status"] != "completed"
        or review_invocation["status"] != "completed"
    ):
        raise Conflict("invalid_finalization_binding")
    draft = journal.output(draft_invocation)
    review = journal.output(review_invocation)

    try:
        action = review_action(review["output"])
    except RunFailure:
        raise Conflict("invalid_finalization_decision") from None
    if (
        action != "keep"
        or record["source_script_ref"]
        != RuntimeRef(
            record_kind="tool_execution", record_id=draft_invocation["output_record_id"]
        ).model_dump(mode="json")
        or record["review_ref"]
        != RuntimeRef(
            record_kind="tool_execution", record_id=review_invocation["output_record_id"]
        ).model_dump(mode="json")
        or record["output"] != draft["output"]
    ):
        raise Conflict("invalid_finalization_binding")
    return record


def finalization_view(record, ref):
    return {key: record[key] for key in ("origin", "model_call", "source_script_ref", "review_ref")} | dict(
        final_ref=ref.model_dump(mode="json")
    )
