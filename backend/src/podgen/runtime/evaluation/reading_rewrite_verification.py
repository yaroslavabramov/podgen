"""Verify a native rewrite against its exact source script and commissioned readings."""

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

from .reading_workflow import round_stage


def verify_rewrite(journal, run_id, index, previous, script, readings, reading_refs, manifest):
    from .editorial_continuation.source import offset
    from .reading_cycle import combine
    from .script_revision import enabled, verify_record

    rewrite = journal.invocation(run_id, round_stage("revise", index))
    if combine(readings)["recommendation"] != "revise" or not rewrite or rewrite["status"] != "completed":
        raise Conflict("independent_round_rewrite_missing")
    rewritten = journal.output(rewrite)
    if rewritten["script_ref"] != previous or rewritten["reading_refs"] != reading_refs:
        raise Conflict("independent_round_rewrite_mismatch")
    if enabled(manifest):
        from podgen.writing.revision.context import from_manifest as revision_context_from_manifest

        from .revision_invocation import source_state

        verify_record(
            rewritten,
            script=script,
            composition=source_state(journal, previous, script),
            readings=readings,
            speakers={row["speaker"] for row in script["utterances"]},
            version=offset(manifest) + index - 1,
            revision_context_contract=revision_context_from_manifest(manifest),
            unresolved_questions=journal.read(
                RuntimeRef(record_kind="tool_execution", record_id=rewrite["request_record_id"])
            )["context"]["unresolved_questions"],
        )
    return RuntimeRef(record_kind="tool_execution", record_id=rewrite["output_record_id"]).model_dump(
        mode="json"
    )
