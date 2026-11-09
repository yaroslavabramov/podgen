"""Recover the precise assignment the writer saw from a completed native revision."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.writing.projections import indexed_draft
from podgen.writing.revision.verification import comparison

from .request_payload import request_payload


def load(journal, run_id, stage, *, script_ref=None, script=None):
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
    return current, comparison(before, current, writer_input["revision_guidance"])


def verify_saved(journal, run_id, stage, script_ref, script, invocation, context, output):
    from podgen.writing.revision.verification import VERSION, RevisionReading

    _, expected = load(journal, run_id, stage, script_ref=script_ref, script=script)
    sent = request_payload(journal.request(invocation))
    if sent.get("revision_check") != expected or context.get("revision_verification") != dict(
        contract=VERSION, comparison_hash=digest(expected)
    ):
        raise Conflict("revision_verification_binding_mismatch")
    RevisionReading.model_validate(output).verify_assignments(expected)
