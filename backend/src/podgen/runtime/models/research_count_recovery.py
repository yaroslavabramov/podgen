"""Retry only a captured, definitive count rejection, retaining its original charge."""

from uuid import uuid5

from podgen.persistence.private_journal.contracts import operation_id
from podgen.runtime.evaluation.turn_preparation import stage_candidate


def candidate(context, phase, body, index, count_identity, admission_refs):
    retry = context.history.retryable_count_error(count_identity, index)
    step = uuid5(context.root["invocation_id"], f"source-turn:{index}")
    if retry is None:
        stage_candidate(phase, body, index, context.original_record, admission_refs=admission_refs)
        return operation_id(step, "count", count_identity), {}
    from podgen.runtime.evaluation.model_turns import TurnJournal

    owner = TurnJournal(context.journal, context.root, index)
    # Verify the already persisted physical request without changing the owner
    # of root context events. Their turn_index binds the checkpoint's step.
    owner.stage_prepare(body, original_record=context.original_record)
    operation = operation_id(step, "count", count_identity, str(retry.record_id))
    return operation, dict(
        count_operation_id=str(operation),
        retry_error_ref=retry.model_dump(mode="json"),
    )


def failure_data(fault, intent, operation, count_identity, request_hash, index):
    return dict(
        request_hash=request_hash,
        code=fault.code,
        ambiguous=fault.ambiguous,
        retryable=not fault.ambiguous
        and fault.reply is not None
        and (fault.reply.status_code == 408 or 500 <= fault.reply.status_code <= 599),
        count_identity=count_identity,
        count_operation_id=str(operation),
        count_intent_ref=intent.model_dump(mode="json"),
        turn_index=index,
    )
