"""Only writer of run-bound execution records; semantic refs require a prepared binder."""

from podgen.domain.canonical import canonical_bytes
from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import walk_values
from podgen.persistence.access_facts import access_facts
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.classification import protect_owned
from podgen.persistence.database import json_text, one, query
from podgen.persistence.read_authorization import invalidate_publication_facts

from .control import Controls
from .live import authorize
from .objects import OwnedBlob
from .parents import authorize_parents


def append(repo, tx, project, run_id, set_id, records, owned_blobs):
    """No semantic graph loading, payload projection trigger or broad classification."""
    if not records or len(records) > 64:
        raise ContractError("invalid_execution_publication_size")
    owner = one(
        tx,
        """SELECT s.generation,r.actor_id FROM execution_input_sets s
        JOIN evaluation_writing_runs r USING(project_id,run_id)
        WHERE s.project_id=:p AND s.run_id=:r AND s.set_id=:s""",
        p=project,
        r=run_id,
        s=set_id,
    )
    if owner is None or owner["actor_id"] != repo.actor_id or repo.purpose != "evaluation":
        raise AccessDenied()
    if repo._acl(tx, project, write=True) not in ("owner", "evaluator", "operator"):
        raise AccessDenied()
    authorize(repo, tx, project, set_id)
    identities = {ref.record_id: ref for ref, _, _ in records}
    if len(identities) != len(records):
        raise Conflict("duplicate_execution_identity")
    existing = {
        row["record_id"]: row
        for row in query(
            tx,
            "SELECT * FROM runtime_records WHERE project_id=:p AND record_id=ANY(CAST(:ids AS uuid[]))",
            p=project,
            ids=list(identities),
        ).mappings()
    }
    refs = tuple(dict.fromkeys(linked for _, payload, _ in records for _, _, linked in walk_values(payload)))
    external = [ref for ref in refs if not (isinstance(ref, RuntimeRef) and ref.record_id in identities)]
    artifacts = [str(ref.revision_id) for ref in external if isinstance(ref, ArtifactRef)]
    runtimes = [ref.record_id for ref in external if isinstance(ref, RuntimeRef)]
    blobs = [ref for ref in refs if isinstance(ref, BlobRef)]
    resources = list(
        query(
            tx,
            """SELECT resource_kind,identity,binding FROM execution_input_resources
        WHERE project_id=:p AND run_id=:r AND set_id=:s AND purpose='evaluation' AND read_required
          AND ((resource_kind='artifact' AND identity=ANY(CAST(:artifacts AS text[])))
            OR (resource_kind='runtime' AND identity=ANY(CAST(CAST(:runtimes AS uuid[]) AS text[])))
            OR (resource_kind='blob' AND owner_project_id=:p AND identity=ANY(CAST(:keys AS text[]))))""",
            p=project,
            r=run_id,
            s=set_id,
            artifacts=artifacts,
            runtimes=runtimes,
            keys=[ref.storage_key for ref in blobs],
        ).mappings()
    )
    covered = {(row["resource_kind"], row["identity"]): row["binding"] for row in resources}
    controls = Controls(tx, project, run_id, set_id, owner["generation"], records).collect(records)
    if any(not isinstance(blob, OwnedBlob) for blob in owned_blobs):
        raise ContractError("execution_unverified_owned_bytes")
    own = {blob.reference for blob in owned_blobs}
    for consumer, payload, _ in records:
        for _, path, ref in walk_values(payload):
            if isinstance(ref, ArtifactRef):
                valid = covered.get(("artifact", str(ref.revision_id))) == ref.model_dump(mode="json")
            elif isinstance(ref, RuntimeRef):
                valid = controls.allowed.get((consumer.record_id, path)) == ref or covered.get(
                    ("runtime", str(ref.record_id))
                ) == ref.model_dump(mode="json")
            else:
                valid = ref in own or covered.get(("blob", ref.storage_key)) == ref.model_dump(mode="json")
                if not valid:
                    valid = controls.blob_owner(consumer.record_id, path, ref)
            if not valid:
                raise ContractError("execution_dependency_not_bound")
    external_parents = [ref.record_id for _, ref in controls.edges if ref.record_id not in identities]
    authorize_parents(repo, tx, project, external_parents, include_parents=False)
    facts = access_facts(repo, tx)
    facts.blobs.load(project, [ref.storage_key for ref in blobs])
    for ref in blobs:
        facts.blobs.check(project, ref)
    earlier, pending, bindings, hashes = set(), [], [], {}
    for ref, payload, replay in records:
        canonical_bytes(payload)
        old = existing.get(ref.record_id)
        if old:
            if (
                not replay
                or old["record_kind"] != ref.record_kind
                or old["payload"] != payload
                or old["audience"] != "evaluator"
                or old["execution_run_id"] != run_id
                or old["execution_input_set_id"] != set_id
            ):
                raise Conflict("execution_id_reused_with_different_input")
        for _, _, linked in walk_values(payload):
            if isinstance(linked, RuntimeRef) and linked.record_id in identities:
                if linked.record_id not in earlier or identities[linked.record_id] != linked:
                    raise ContractError("invalid_execution_dependency_order")
        earlier.add(ref.record_id)
        if old:
            continue
        pending.append(
            dict(
                project_id=project,
                record_id=ref.record_id,
                record_kind=ref.record_kind,
                payload=json_text(payload),
                audience="evaluator",
                execution_run_id=run_id,
                execution_input_set_id=set_id,
            )
        )
        for linked in dict.fromkeys(v for _, _, v in walk_values(payload) if isinstance(v, BlobRef)):
            owned = linked in own
            bindings.append(
                dict(
                    project_id=project,
                    record_id=ref.record_id,
                    storage_key=linked.storage_key,
                    sha256=linked.sha256,
                    byte_length=linked.byte_length,
                    media_type=linked.media_type,
                    is_owned=owned,
                )
            )
            if owned:
                hashes.setdefault(linked.sha256, ref.record_id)
    insert_rows(tx, "runtime_records", pending, json_columns=("payload",))
    insert_rows(tx, "runtime_blob_refs", bindings)
    insert_rows(
        tx,
        "runtime_runtime_refs",
        [
            dict(
                project_id=project,
                record_id=consumer,
                target_record_id=parent.record_id,
                record_kind=parent.record_kind,
            )
            for consumer, parent in dict.fromkeys(controls.edges)
        ],
        on_conflict=True,
    )
    results = []
    for ref, payload, _ in records:
        if ref.record_id in existing or payload.get("event_kind") != "source_tool_result":
            continue
        data = payload.get("data", {})
        response = data.get("provider_response_ref")
        if response is None:
            continue
        from uuid import UUID

        proof = controls.calls[UUID(response["record_id"]), data["call_id"]]
        results.append(
            dict(
                project_id=project,
                run_id=run_id,
                physical_invocation_id=proof["physical_invocation_id"],
                call_id=data["call_id"],
                arguments_hash=data["arguments_hash"],
                response_record_id=UUID(response["record_id"]),
                record_id=ref.record_id,
            )
        )
    insert_rows(tx, "execution_tool_results", results)
    invalidate_publication_facts(repo, tx)
    protect_owned(tx, project, hashes)
    invalidate_publication_facts(repo, tx)
