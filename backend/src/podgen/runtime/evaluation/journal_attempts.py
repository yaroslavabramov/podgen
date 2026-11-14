"""Attempt preparation uses private facts; historical invocation rows stay read-only."""

import json
from dataclasses import replace
from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.references import walk_values
from podgen.persistence.database import one, query
from podgen.persistence.private_journal.contracts import Checkpoint, HeadChange, operation_id
from podgen.persistence.private_journal.head import next_head
from podgen.persistence.private_journal.objects import prepare as prepare_bytes
from podgen.persistence.private_journal.writer import commit
from podgen.persistence.read_authorization import _read_connection

from .attempts import attempt_identity, validate_preparation


def prepare(
    journal,
    run_id,
    stage,
    body,
    context,
    *,
    attempt_index=0,
    parent_invocation_id=None,
    attempt_kind=None,
    operation_kind=None,
    authorization=None,
):
    rows = journal.attempts(run_id, stage)
    existing = next((row for row in rows if row["attempt_index"] == attempt_index), None)
    if existing:
        return existing
    attempt_kind = attempt_kind or ("anchor_repair" if attempt_index else "role")
    operation_kind = operation_kind or ("anchor_repair" if attempt_kind == "anchor_repair" else "role")
    parent = validate_preparation(
        rows, stage, attempt_index, attempt_kind, operation_kind, parent_invocation_id
    )
    identity = attempt_identity(run_id, stage, attempt_index, attempt_kind, parent)
    raw = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    obj = prepare_bytes(raw, journal.project)
    parent_ref = (
        RuntimeRef(record_kind="tool_execution", record_id=parent["request_record_id"]) if parent else None
    )
    bound = journal.execution.prepare_context(
        run_id,
        dict(
            context=context,
            authorization=authorization,
            parent_request_ref=parent_ref.model_dump(mode="json") if parent_ref else None,
        ),
    )
    parents, replacement = set(), None
    if parent:
        with _read_connection(journal.repo) as tx:
            ids = list(
                {
                    ref.record_id
                    for _, _, ref in walk_values(
                        dict(
                            context=context,
                            authorization=authorization,
                            parent_request_ref=parent_ref.model_dump(mode="json"),
                        )
                    )
                    if isinstance(ref, RuntimeRef)
                }
            )
            owners = (
                query(
                    tx,
                    "SELECT entry_id FROM execution_entries WHERE project_id=:p AND run_id=:r AND archive_ids && CAST(:ids AS uuid[])",
                    p=journal.project,
                    r=run_id,
                    ids=ids,
                )
                .scalars()
                .all()
            )
            parents.update(owners)
            if attempt_kind == "technical_retry":
                target = one(
                    tx,
                    "SELECT entry_id FROM execution_entries WHERE project_id=:p AND run_id=:r AND attempt_id=:attempt ORDER BY revision DESC LIMIT 1",
                    p=journal.project,
                    r=run_id,
                    attempt=parent["invocation_id"],
                )
                replacement = target["entry_id"] if target else None
                if replacement:
                    parents.add(replacement)
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(identity, "request"))
    request = dict(
        contract="model-request-v1",
        journal_version="execution-journal/3",
        invocation_id=str(identity),
        run_id=str(run_id),
        stage=stage,
        request_blob=obj.reference.model_dump(mode="json"),
        request_hash=obj.reference.sha256,
        context=context,
        attempt_kind=attempt_kind,
        operation_kind=operation_kind,
        parent_request_ref=RuntimeRef(
            record_kind="tool_execution", record_id=parent["request_record_id"]
        ).model_dump(mode="json")
        if parent
        else None,
        authorization=authorization,
    )
    invocation = dict(
        project_id=journal.project,
        run_id=run_id,
        invocation_id=identity,
        stage=stage,
        status="prepared",
        request_record_id=ref.record_id,
        attempt_index=attempt_index,
        parent_invocation_id=parent_invocation_id,
        parent_attempt_index=parent["attempt_index"] if parent else None,
        attempt_kind=attempt_kind,
        operation_kind=operation_kind,
        provider_response_id=None,
        input_tokens=None,
        output_tokens=None,
        output_record_id=None,
    )
    previous = journal.execution.current_head(run_id)
    command = Checkpoint(
        run_id=run_id,
        attempt_id=identity,
        operation_id=operation_id(identity, "prepare"),
        kind="authorized_replacement" if attempt_kind == "technical_retry" else "attempt_prepared",
        input_set_id=bound["set_id"],
        expected_revision=previous["revision"] if previous else 0,
        data=dict(
            attempt={
                key: str(value) if hasattr(value, "hex") else value for key, value in invocation.items()
            },
            stage=stage,
            authorization=authorization,
            replacement_entry_id=str(replacement) if replacement else None,
        ),
        parents=tuple(sorted(parents, key=str)),
        objects=dict(request=obj),
        views=(dict(record_id=str(ref.record_id), record_kind=ref.record_kind, payload=request),),
        head=HeadChange(phase="prepared", request_hash=obj.reference.sha256),
    )
    command = replace(
        command,
        head=HeadChange(
            phase="prepared", request_entry_id=command.entry_id, request_hash=obj.reference.sha256
        ),
    )
    commit(journal.repo, journal.project, command)
    journal.execution.committed(run_id, next_head(previous, command))
    journal.execution.remember_checkpoint(command)
    return invocation
