"""Verify final decisions against both readings of the exact saved version; render human results."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one
from podgen.writing.revision import verification

from .reading_cycle import combine, outcome_ref
from .reading_workflow import VERSION, round_stage


def read_outcome(journal, run_id):
    ref = outcome_ref(run_id)
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
    outcome = journal.read(ref)
    manifest = journal.manifest(run_id)
    if (
        outcome.get("contract") != "independent-reading-outcome/1"
        or outcome.get("review_workflow") != VERSION
        or manifest.get("review_workflow") != VERSION
        or outcome.get("run_id") != str(run_id)
    ):
        raise Conflict("invalid_independent_outcome")
    previous = manifest["command"].get("initial_script_ref") or outcome["output_refs"]["draft"]
    for index, round_ref in enumerate(outcome["rounds"]):
        record = journal.read(RuntimeRef.model_validate(round_ref))
        script = journal.read(RuntimeRef.model_validate(record["script_ref"]))["output"]
        if (
            record["run_id"] != str(run_id)
            or record["index"] != index
            or record["script_ref"] != previous
            or record["script_hash"] != digest(script)
        ):
            raise Conflict("independent_round_script_mismatch")
        readings = {}
        for origin, base in (("reader", "reader-review"), ("fact", "fact-review")):
            stage = round_stage(base, index)
            invocation = journal.invocation(run_id, stage)
            if not invocation or invocation["status"] != "completed":
                raise Conflict("independent_round_reading_missing")
            expected = RuntimeRef(
                record_kind="tool_execution", record_id=invocation["output_record_id"]
            ).model_dump(mode="json")
            output = journal.output(invocation)
            request = journal.read(
                RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
            )
            if (
                record["reading_refs"][origin] != expected
                or output["script_ref"] != previous
                or output["script_hash"] != digest(script)
                or request["context"]["script_ref"] != previous
                or request["context"]["script_hash"] != digest(script)
            ):
                raise Conflict("independent_round_reading_mismatch")
            if verification.applies(stage, manifest.get(verification.KEY)):
                from .recheck_source import verify_saved

                verify_saved(
                    journal,
                    run_id,
                    stage,
                    previous,
                    script,
                    invocation,
                    request["context"],
                    output["output"],
                    version=manifest[verification.KEY],
                )
            readings[origin] = output["output"]
        if record["decision"] != combine(readings):
            raise Conflict("independent_round_decision_mismatch")
        if index < len(outcome["rounds"]) - 1:
            rewrite = journal.invocation(run_id, round_stage("revise", index + 1))
            if (
                record["decision"]["recommendation"] != "revise"
                or not rewrite
                or rewrite["status"] != "completed"
            ):
                raise Conflict("independent_round_rewrite_missing")
            rewritten = journal.output(rewrite)
            if rewritten["script_ref"] != previous or rewritten["reading_refs"] != record["reading_refs"]:
                raise Conflict("independent_round_rewrite_mismatch")
            from .script_revision import enabled, verify_record

            if enabled(manifest):
                from podgen.writing.revision.context import from_manifest as revision_context_from_manifest

                from .revision_invocation import source_state

                verify_record(
                    rewritten,
                    script=script,
                    composition=source_state(journal, previous, script),
                    readings=readings,
                    speakers={row["speaker"] for row in script["utterances"]},
                    version=index,
                    revision_context_contract=revision_context_from_manifest(manifest),
                    unresolved_questions=journal.read(
                        RuntimeRef(record_kind="tool_execution", record_id=rewrite["request_record_id"])
                    )["context"]["unresolved_questions"],
                )
            previous = RuntimeRef(
                record_kind="tool_execution", record_id=rewrite["output_record_id"]
            ).model_dump(mode="json")
    if not outcome["rounds"] or len(outcome["rounds"]) > manifest["command"]["max_script_revisions"] + 1:
        raise Conflict("independent_round_limit_mismatch")
    expected_status = record["decision"]["recommendation"]
    if expected_status == "revise":
        if len(outcome["rounds"]) != manifest["command"]["max_script_revisions"] + 1:
            raise Conflict("independent_round_limit_mismatch")
        expected_status = "revision_limit_exhausted"
    if (
        outcome["status"] != expected_status
        or outcome["script_ref"] != previous
        or outcome["script_hash"] != digest(script)
        or outcome["output"] != script
        or outcome["reading_refs"] != record["reading_refs"]
    ):
        raise Conflict("independent_final_script_mismatch")
    return outcome


def outcome_result(outcome, command):
    kept = outcome["status"] == "keep"
    return dict(
        run_id=str(command.run_id),
        case_id=command.case_id,
        status="completed_unaccepted" if kept else "needs_attention",
        review_status=outcome["status"],
        failure_code=None if kept else "independent_review_" + outcome["status"],
        output_refs=outcome["output_refs"],
        final_script_ref=outcome["script_ref"],
        outcome_ref=outcome_ref(command.run_id).model_dump(mode="json"),
        token_usage_or_reservation=outcome["token_usage_or_reservation"],
        semantic_acceptance=False,
        production_ready=False,
    )
