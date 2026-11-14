"""Verify final decisions against both readings of the exact saved version; render human results."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one
from podgen.writing.reading_audit.consistency import from_manifest as consistency_from_manifest
from podgen.writing.revision import verification

from .reading_cycle import combine, outcome_ref
from .reading_workflow import VERSION, round_stage


def audit_outcome(journal, run_id):
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
    from .editorial_continuation.source import KEY, offset, seed
    from .reading_rewrite_verification import verify_rewrite

    audit_materials = None
    if manifest.get("reading_audit_contract") is not None:
        from .reading_audit.replay import load_materials

        audit_materials = load_materials(journal, manifest)

    start = 1 if KEY in manifest else 0
    previous = manifest["command"].get("initial_script_ref") or outcome["output_refs"]["draft"]
    if start:
        original, previous, readings, refs = seed(journal, manifest)
        previous = verify_rewrite(journal, run_id, 1, previous, original, readings, refs, manifest)
    for index, round_ref in enumerate(outcome["rounds"], start=start):
        record = journal.read(RuntimeRef.model_validate(round_ref))
        script = journal.read(RuntimeRef.model_validate(record["script_ref"]))["output"]
        if (
            record["run_id"] != str(run_id)
            or record["index"] != index
            or record["script_ref"] != previous
            or record["script_hash"] != digest(script)
            or start
            and record.get("global_revision_index") != offset(manifest) + index
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
            if manifest.get("reading_audit_contract") is not None:
                from .reading_audit.replay import verify_outcome_reading

                verify_outcome_reading(
                    journal,
                    run_id,
                    stage,
                    manifest,
                    script,
                    previous,
                    invocation,
                    request["context"],
                    output,
                    audit_materials,
                )
            elif verification.applies(stage, manifest.get(verification.KEY)):
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
        if record["decision"] != combine(readings, consistency_contract=consistency_from_manifest(manifest)):
            raise Conflict("independent_round_decision_mismatch")
        if index < start + len(outcome["rounds"]) - 1:
            previous = verify_rewrite(
                journal, run_id, index + 1, previous, script, readings, record["reading_refs"], manifest
            )
    if (
        not outcome["rounds"]
        or len(outcome["rounds"]) > manifest["command"]["max_script_revisions"] + 1 - start
    ):
        raise Conflict("independent_round_limit_mismatch")
    expected_status = record["decision"]["recommendation"]
    if expected_status == "revise":
        if len(outcome["rounds"]) != manifest["command"]["max_script_revisions"] + 1 - start:
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
    if start:
        from .editorial_continuation.budget import split_usage

        if (
            outcome.get("revisions_completed") != offset(manifest) + index
            or outcome.get("max_total_revisions") != manifest[KEY]["max_total_revisions"]
            or outcome.get("usage_ledger") != split_usage(manifest, outcome["token_usage_or_reservation"])
        ):
            raise Conflict("editorial_continuation_outcome_mismatch")
    return outcome
