from uuid import UUID, uuid4

from pydantic import TypeAdapter

from podgen.domain.canonical import canonical_bytes, content_hash, digest, sorted_refs
from podgen.domain.contracts import ArtifactRef, BlobRef, validate_payload
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import LINEAGE_FIELDS, check_subtype

from .database import json_text, one, query
from .repository import CONFIG_KINDS, HIDDEN_KINDS, timestamp


def create_revision(
    repo,
    tx,
    project_id,
    kind,
    payload,
    *,
    producer,
    operation_id,
    input_refs=(),
    artifact_id=None,
    episode_id=None,
    audience="project",
    streaming=False,
    prepared=None,
):
    repo._acl(tx, project_id, write=True)
    repo._lock(tx, project_id)
    if audience not in ("project", "evaluator"):
        raise ContractError("invalid_audience")
    validate_payload(kind, payload)
    if not streaming and len(canonical_bytes(payload)) > repo.inline_limit:
        raise ContractError("payload_too_large", "Use the full artifact document streaming import route.")
    producer_dict = producer.model_dump(mode="json") if hasattr(producer, "model_dump") else producer

    from podgen.domain.contracts.common import ProducerRef

    TypeAdapter(ProducerRef).validate_python(producer_dict)
    if kind in HIDDEN_KINDS:
        audience = "evaluator"
    if audience == "evaluator" and (
        repo.purpose != "evaluation" or repo._acl(tx, project_id) not in ("owner", "evaluator", "operator")
    ):
        raise AccessDenied()
    from .repository import Repository

    public_reader = (
        (
            repo
            if repo.purpose == "generation"
            else Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
        )
        if audience == "project"
        else None
    )
    if producer_dict["kind"] == "model" and producer_dict["profile_ref"]["kind"] != "ModelProfile":
        raise ContractError("producer_profile_kind_mismatch")
    from .artifact_inputs import ArtifactInputs

    inputs = ArtifactInputs(repo, tx, project_id, payload, input_refs, producer_dict, public_reader)
    with inputs.authorized():
        return _publish(
            repo,
            tx,
            project_id,
            kind,
            payload,
            producer_dict,
            operation_id,
            artifact_id,
            episode_id,
            audience,
            inputs,
            prepared,
        )


def _publish(
    repo,
    tx,
    project_id,
    kind,
    payload,
    producer_dict,
    operation_id,
    artifact_id,
    episode_id,
    audience,
    inputs,
    prepared,
):
    refs, blobs = {}, []
    for field, path, ref in inputs.occurrences:
        if isinstance(ref, ArtifactRef):
            if ref.revision_id not in refs:
                refs[ref.revision_id] = (ref, set(), inputs.resolve(ref))
            entry = refs[ref.revision_id]
            check_subtype(field, entry[2]["payload"])
            entry[1].add(field)
        elif isinstance(ref, BlobRef):
            if prepared is None:
                raise ContractError("artifact_bytes_not_prepared")
            prepared.require(repo, project_id, ref)
            blobs.append((path, ref))
    runtimes = inputs.runtimes
    all_refs = sorted_refs([v[0] for v in refs.values()])
    if kind == "SourceRevision":
        source_id = UUID(payload["source_id"])
        if artifact_id is not None and artifact_id != source_id:
            raise ContractError("source_identity_mismatch")
        artifact_id = source_id
    fingerprint = digest(
        dict(
            kind=kind,
            payload=payload,
            input_refs=[r.model_dump(mode="json") for r in all_refs],
            producer=producer_dict,
            artifact_id=str(artifact_id) if artifact_id else None,
            episode_id=str(episode_id) if episode_id else None,
            audience=audience,
        )
    )
    previous = one(
        tx,
        "SELECT * FROM artifact_revisions WHERE project_id=:p AND operation_id=:o",
        p=project_id,
        o=operation_id,
    )
    if previous:
        if previous["operation_fingerprint"] != fingerprint:
            raise Conflict("operation_id_reused_with_different_input")
        readable = repo._resolve(
            tx,
            project_id,
            ArtifactRef(
                artifact_id=previous["artifact_id"],
                revision_id=previous["revision_id"],
                kind=previous["kind"],
            ),
        )
        repo._verify_hash(tx, readable)
        return repo._envelope(readable)
    if payload.get("parent_script_ref") is not None:
        parent = ArtifactRef.model_validate(payload["parent_script_ref"])
        if artifact_id != parent.artifact_id:
            raise ContractError("parent_script_identity_mismatch")
    repo._validate_bindings(tx, project_id, kind, payload, refs, inputs=inputs, prepared=prepared)
    artifact_id = artifact_id or uuid4()
    existing = one(
        tx, "SELECT kind FROM artifacts WHERE project_id=:p AND artifact_id=:a", p=project_id, a=artifact_id
    )
    if existing and existing["kind"] != kind:
        raise Conflict("artifact_kind_is_immutable")
    if not existing:
        query(tx, "INSERT INTO artifacts VALUES(:p,:a,:k)", p=project_id, a=artifact_id, k=kind)
    ref = ArtifactRef(artifact_id=artifact_id, revision_id=uuid4(), kind=kind)
    checksum = content_hash(
        kind, "1.0", payload, all_refs, [dict(purpose=path, hash=b.sha256) for path, b in blobs]
    )
    query(
        tx,
        """INSERT INTO artifact_revisions(project_id,revision_id,artifact_id,kind,episode_id,schema_version,
                 payload,input_refs,producer,content_hash,created_at,audience,operation_id,operation_fingerprint)
                 VALUES(:p,:r,:a,:k,:e,'1.0',CAST(:body AS json),CAST(:refs AS jsonb),CAST(:producer AS jsonb),
                 :h,:time,:aud,:op,:fp)""",
        p=project_id,
        r=ref.revision_id,
        a=artifact_id,
        k=kind,
        e=episode_id,
        body=json_text(payload),
        refs=json_text([r.model_dump(mode="json") for r in all_refs]),
        producer=json_text(producer_dict),
        h=checksum,
        time=timestamp(),
        aud=audience,
        op=operation_id,
        fp=fingerprint,
    )
    for target, fields, row in refs.values():
        if row["project_id"] != project_id:
            publication = repo._publication(tx, project_id, row)
            query(
                tx,
                "INSERT INTO catalog_references VALUES(:p,:c,:pub)",
                p=project_id,
                c=ref.revision_id,
                pub=publication["publication_id"],
            )
            continue
        query(
            tx,
            "INSERT INTO revision_links VALUES(:p,:c,:i)",
            p=project_id,
            c=ref.revision_id,
            i=target.revision_id,
        )
        relations = {
            "lineage"
            if field in LINEAGE_FIELDS
            or kind == "SourceRevision"
            and target.artifact_id == artifact_id
            and field == "input_refs"
            else "applies_policy"
            if target.kind in CONFIG_KINDS
            else "uses_content"
            for field in fields
        }
        for relation in sorted(relations):
            repo._edge(tx, project_id, ref.revision_id, target.revision_id, relation)
        if fields & LINEAGE_FIELDS and kind in ("ScriptRevision", "ResearchNote", "StoryPlan"):
            for inherited in repo._effective_edges(tx, project_id, target.revision_id):
                if inherited["relation"] != "lineage":
                    repo._edge(tx, project_id, ref.revision_id, inherited["input_id"], inherited["relation"])
    for path, blob in blobs:
        query(
            tx,
            "INSERT INTO artifact_blobs VALUES(:p,:r,:k,:purpose)",
            p=project_id,
            r=ref.revision_id,
            k=blob.storage_key,
            purpose=path,
        )
    for runtime in {r.record_id: r for r in runtimes}.values():
        query(
            tx,
            "INSERT INTO runtime_links VALUES(:p,:r,:id,:k)",
            p=project_id,
            r=ref.revision_id,
            id=runtime.record_id,
            k=runtime.record_kind,
        )
    from .read_authorization import invalidate_publication_facts

    invalidate_publication_facts(repo, tx)
    if audience == "evaluator":
        from .classification import classify_evaluator

        classify_evaluator(tx, project_id, artifact_id=ref.revision_id)
    repo._bump(tx, project_id)
    query(
        tx,
        "INSERT INTO outbox(event_id,project_id,event_kind,payload) VALUES(:id,:p,'artifact_created',CAST(:body AS json))",
        id=uuid4(),
        p=project_id,
        body=json_text(ref),
    )
    return repo._envelope(repo._row(tx, ref))
