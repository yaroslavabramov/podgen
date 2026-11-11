"""Recover the precise assignment the writer saw from a completed native revision."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.writing.projections import indexed_draft
from podgen.writing.revision.verification import TASK_VERSION, VERSION, comparison

from .request_payload import request_payload


def load(journal, run_id, stage, *, version=VERSION, script_ref=None, script=None):
    if version not in (VERSION, TASK_VERSION):
        raise ContractError("unknown_revision_verification_contract")
    revision_stage = stage.replace("reader-review", "revise")
    invocation = journal.invocation(run_id, revision_stage)
    if invocation is None or invocation["status"] != "completed":
        raise Conflict("revision_verification_source_missing")
    record = journal.output(invocation)
    ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["output_record_id"])
    current = record["output"]
    before = journal.read(RuntimeRef.model_validate(record["script_ref"]))["output"]
    request = journal.request(invocation)
    writer_input = request_payload(request)
    if (
        (script_ref is not None and ref.model_dump(mode="json") != script_ref)
        or (script is not None and current != script)
        or record["stage"] != revision_stage
        or record["run_id"] != str(run_id)
        or record["script_hash"] != digest(before)
        or record["request_ref"]["record_id"] != str(invocation["request_record_id"])
        or writer_input["script"] != indexed_draft(before)
    ):
        raise Conflict("revision_verification_source_mismatch")
    if version == VERSION:
        return current, comparison(before, current, writer_input["revision_guidance"])
    from podgen.writing.revision.task_verification import comparison as task_comparison

    decision = previous_reader_decision(journal, run_id, revision_stage, record, before)
    return current, task_comparison(before, current, writer_input, decision)


def previous_reader_decision(journal, run_id, revision_stage, revision, before):
    number = int(revision_stage.rsplit("-", 1)[1]) - 1
    stage = "reader-review" if number == 0 else f"reader-review-{number}"
    source_run, seed_ref = run_id, None
    if number == 0:
        from .editorial_continuation.source import previous_reader_source

        inherited = previous_reader_source(journal, run_id)
        if inherited is not None:
            source_run, stage, seed_ref = inherited
    invocation = journal.invocation(source_run, stage)
    if invocation is None or invocation["status"] != "completed":
        raise Conflict("revision_verification_previous_reader_missing")
    expected_ref = RuntimeRef(
        record_kind="tool_execution", record_id=invocation["output_record_id"]
    ).model_dump(mode="json")
    record = journal.output(invocation)
    request = journal.read(RuntimeRef.model_validate(record["request_ref"]))
    if (
        revision["reading_refs"]["reader"] != expected_ref
        or seed_ref is not None and seed_ref != expected_ref
        or record["script_ref"] != revision["script_ref"]
        or record["script_hash"] != digest(before)
        or record["stage"] != stage
        or record["run_id"] != str(source_run)
        or record["request_ref"]["record_id"] != str(invocation["request_record_id"])
        or request["context"]["script_ref"] != revision["script_ref"]
        or request["context"]["script_hash"] != digest(before)
    ):
        raise Conflict("revision_verification_previous_reader_mismatch")
    return record["output"]


def verify_saved(journal, run_id, stage, script_ref, script, invocation, context, output, *, version=VERSION):
    from podgen.writing.revision.verification import feedback_model

    from .recheck_request import semantic_request

    _, expected = load(journal, run_id, stage, version=version, script_ref=script_ref, script=script)
    sent = request_payload(semantic_request(journal, run_id, stage, invocation, context, script, output))
    if sent.get("revision_check") != expected or context.get("revision_verification") != dict(
        contract=version, comparison_hash=digest(expected)
    ):
        raise Conflict("revision_verification_binding_mismatch")
    parsed = feedback_model(version).model_validate(output)
    parsed.verify_assignments(expected)
    if version == TASK_VERSION:
        from podgen.writing.roles import WriteOutput

        parsed.validate_against(WriteOutput.model_validate(script))
