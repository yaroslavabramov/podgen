"""Publish an actual role result atomically with its private execution provenance."""

from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.domain.references import walk_values
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import query
from podgen.persistence.read_authorization import invalidate_publication_facts
from podgen.persistence.runtime_records import append_runtime

from . import access

LOCATOR = "$podgen_execution_ref"


def _transform(value, aliases):
    if isinstance(value, dict):
        if LOCATOR in value:
            raise ContractError("reserved_execution_locator_in_semantic_input")
        if set(value) == {"record_id", "record_kind"}:
            reference = RuntimeRef.model_validate(value)
            target = aliases.get(reference.record_id)
            if target is not None:
                entry, kind = target
                if kind != reference.record_kind:
                    raise ContractError("semantic_execution_reference_kind_mismatch")
                return {LOCATOR: dict(version=3, entry=str(entry), alias=value["record_id"], kind=kind)}
        return {key: _transform(child, aliases) for key, child in value.items()}
    if isinstance(value, list):
        return [_transform(child, aliases) for child in value]
    return value


def restore_locators(value):
    """Decode only after the caller has authorized the semantic record.

    This restores the evaluator archive view, not a grant to private bytes.
    Each subsequent private read still goes through ExecutionArchiveAdapter.
    """
    if isinstance(value, dict):
        if set(value) == {LOCATOR}:
            locator = value[LOCATOR]
            if not isinstance(locator, dict) or set(locator) != {"version", "entry", "alias", "kind"}:
                raise ContractError("invalid_semantic_execution_locator")
            if locator["version"] != 3:
                raise ContractError("unsupported_semantic_execution_locator")
            UUID(locator["entry"])
            RuntimeRef(record_id=locator["alias"], record_kind=locator["kind"])
            return dict(record_id=locator["alias"], record_kind=locator["kind"])
        return {key: restore_locators(child) for key, child in value.items()}
    if isinstance(value, list):
        return [restore_locators(child) for child in value]
    return value


def publish(repo, tx, project, command):
    """Called inside the checkpoint transaction, after its entry has been inserted."""
    result = command.semantic_result
    if result is None:
        return
    if not isinstance(result.reference, RuntimeRef) or not isinstance(result.payload, dict):
        raise ContractError("invalid_semantic_result")
    if (
        query(
            tx,
            """SELECT 1 FROM execution_entries
        WHERE project_id=:p AND archive_ids @> ARRAY[CAST(:record AS uuid)] LIMIT 1""",
            p=project,
            record=result.reference.record_id,
        ).first()
        is not None
    ):
        raise Conflict("semantic_result_identity_is_private_alias")
    sources = tuple(result.source_entries)
    if (
        not sources
        or any(not isinstance(value, UUID) for value in sources)
        or len(set(sources)) != len(sources)
    ):
        raise ContractError("invalid_semantic_execution_sources")
    if command.entry_id not in sources:
        raise ContractError("semantic_result_missing_own_checkpoint")
    # A complete audit may cite many earlier reads. Its semantic provenance is
    # separate from the few control parents of an individual external operation.
    for offset in range(0, len(sources), 64):
        batch = sources[offset : offset + 64]
        rows = list(
            query(
                tx,
                """SELECT entry_id,run_id,input_set_id FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND entry_id=ANY(CAST(:ids AS uuid[]))""",
                p=project,
                r=command.run_id,
                ids=list(batch),
            ).mappings()
        )
        if {row["entry_id"] for row in rows} != set(batch):
            raise ContractError("semantic_execution_source_missing")
        for run_id, set_id in {(row["run_id"], row["input_set_id"]) for row in rows}:
            access.inputs(repo, tx, project, run_id, set_id)
        access.entry_privacy(tx, project, batch)
    references = {ref.record_id for _, _, ref in walk_values(result.payload) if isinstance(ref, RuntimeRef)}
    aliases = {}
    if references:
        for row in query(
            tx,
            """SELECT entry_id,archive_views FROM execution_entries
            WHERE project_id=:p AND archive_ids && CAST(:ids AS uuid[])""",
            p=project,
            ids=list(references),
        ).mappings():
            for view in row["archive_views"]:
                identity = UUID(view["record_id"])
                if identity in references:
                    if row["entry_id"] not in sources:
                        raise ContractError("semantic_execution_reference_not_bound")
                    aliases[identity] = (row["entry_id"], view["record_kind"])
    payload = _transform(result.payload, aliases)
    existing = set(
        query(
            tx,
            """SELECT entry_id FROM semantic_execution_links
        WHERE project_id=:p AND runtime_record_id=:record""",
            p=project,
            record=result.reference.record_id,
        ).scalars()
    )
    if existing and existing != set(sources):
        raise Conflict("semantic_execution_sources_changed")
    append_runtime(repo, tx, project, result.reference, payload, audience="evaluator", replay=True)
    if not existing:
        for offset in range(0, len(sources), 128):
            insert_rows(
                tx,
                "semantic_execution_links",
                [
                    dict(project_id=project, runtime_record_id=result.reference.record_id, entry_id=entry)
                    for entry in sources[offset : offset + 128]
                ],
            )
    from podgen.persistence.publication_scope import AccessChange

    invalidate_publication_facts(
        repo, tx, effect=AccessChange(runtimes=((project, result.reference.record_id),))
    )
