"""A late count rejection cannot settle or reopen another active operation."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.persistence.private_journal.count_recovery import validate


@pytest.mark.parametrize("field", ["active_attempt_id", "active_step_id"])
def test_count_rejection_requires_its_original_active_owner(field):
    attempt, step, operation, alias, entry = (uuid4() for _ in range(5))
    reference = dict(record_kind="tool_execution", record_id=str(alias))
    intent = dict(
        entry_id=entry,
        checkpoint_kind="count_intent",
        attempt_id=attempt,
        step_id=step,
        operation_id=operation,
        archive_views=[dict(record_id=str(alias), event=dict(kind="source_context_count_intent"))],
    )
    command = SimpleNamespace(
        kind="count_error",
        attempt_id=attempt,
        step_id=step,
        operation_id=operation,
        data=dict(count_intent_ref=reference, count_identity="exact-candidate"),
        views=[
            dict(
                payload=dict(
                    event_kind="source_context_count_error",
                    provider_http_status=503,
                    data=dict(ambiguous=False, retryable=True),
                )
            )
        ],
    )
    previous = dict(
        phase="counting", active_attempt_id=attempt, active_step_id=step, count_identity="exact-candidate"
    )
    validate(command, previous, {entry: intent})
    with pytest.raises(ContractError, match="private_count_recovery_evidence_mismatch"):
        validate(command, dict(previous, **{field: uuid4()}), {entry: intent})
