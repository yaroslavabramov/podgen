"""Bounded atomic runtime publication; generic append keeps its original contract."""

from podgen.domain.canonical import canonical_bytes
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import walk_values

from .batch_insert import insert_rows
from .database import json_text, query
from .runtime_records import _validate_inputs

MAX_RECORDS = 64


def append_evaluation_batch(repo, tx, project, records):
    """Validate the union before publishing any record, then classify the union.

    Internal references must point to an earlier record with the exact kind. Their
    external dependencies are included in the same validation. Replay is explicit;
    existing records remain subject to full input authorization.
    """
    if not records or len(records) > MAX_RECORDS:
        raise ContractError("invalid_runtime_publication_size")
    repo._acl(tx, project, write=True)
    repo._lock(tx, project)
    if repo.purpose != "evaluation" or repo._acl(tx, project) not in ("owner", "evaluator", "operator"):
        raise AccessDenied()
    identities = {ref.record_id: ref for ref, _, _ in records}
    if len(identities) != len(records):
        raise Conflict("duplicate_runtime_publication_identity")
    existing = {
        row["record_id"]: row
        for row in query(
            tx,
            "SELECT * FROM runtime_records WHERE project_id=:p AND record_id=ANY(CAST(:ids AS uuid[]))",
            p=project,
            ids=list(identities),
        ).mappings()
    }
    external, earlier, pending = {identities[key]: None for key in existing}, set(), []
    for ref, payload, replay in records:
        canonical_bytes(payload)
        old = existing.get(ref.record_id)
        if old and (
            not replay
            or old["record_kind"] != ref.record_kind
            or old["payload"] != payload
            or old["audience"] != "evaluator"
        ):
            raise Conflict("runtime_id_reused_with_different_input")
        for _, _, linked in walk_values(payload):
            if isinstance(linked, RuntimeRef) and linked.record_id in identities:
                if linked.record_id not in earlier or identities[linked.record_id] != linked:
                    raise ContractError("invalid_runtime_publication_dependency")
            else:
                external[linked] = None
        earlier.add(ref.record_id)
        if not old:
            pending.append(
                dict(
                    project_id=project,
                    record_id=ref.record_id,
                    record_kind=ref.record_kind,
                    payload=json_text(payload),
                    audience="evaluator",
                )
            )
    _validate_inputs(repo, tx, project, [ref.model_dump(mode="json") for ref in external], "evaluator")
    if not pending:
        return
    insert_rows(tx, "runtime_records", pending, json_columns=("payload",))
    # Invalidation surrounds classification: no pre-insert permission fact may be
    # used once new consumers or same-SHA aliases have entered the graph.
    from .read_authorization import invalidate_publication_facts

    invalidate_publication_facts(repo, tx)
    from .classification import classify_evaluator_batch

    classify_evaluator_batch(tx, project, [row["record_id"] for row in pending])
    invalidate_publication_facts(repo, tx)
