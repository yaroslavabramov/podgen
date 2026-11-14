"""Compile validated artifact nodes into canonical rows and relationship batches."""

from uuid import UUID, uuid4

from podgen.domain.canonical import content_hash, digest, sorted_refs
from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.errors import Conflict, ContractError
from podgen.domain.references import LINEAGE_FIELDS, check_subtype

from .database import json_text
from .repository import CONFIG_KINDS, timestamp

TABLES = (
    "artifacts",
    "artifact_revisions",
    "revision_links",
    "artifact_dependencies",
    "artifact_blobs",
    "runtime_links",
    "catalog_references",
    "outbox",
)


def fingerprint(node, payload, refs, producer, artifact_id):
    value = dict(
        kind=node.kind,
        payload=payload,
        input_refs=[ref.model_dump(mode="json") for ref in refs],
        producer=producer,
        artifact_id=str(artifact_id) if artifact_id else None,
        episode_id=str(node.episode_id) if node.episode_id else None,
        audience=node.audience,
    )
    if node.reference_policy != "legacy/1":
        value["reference_policy"] = node.reference_policy
    return digest(value)


def compile_node(repo, tx, project, node, ref, payload, producer, inputs, prepared, existing, groups):
    refs, blobs = {}, []
    for field, path, linked in inputs.occurrences:
        if isinstance(linked, ArtifactRef):
            if linked.revision_id not in refs:
                refs[linked.revision_id] = (linked, set(), inputs.resolve(linked))
            entry = refs[linked.revision_id]
            check_subtype(field, entry[2]["payload"])
            entry[1].add(field)
        elif isinstance(linked, BlobRef):
            if prepared is None:
                raise ContractError("artifact_bytes_not_prepared")
            prepared.require(repo, project, linked)
            blobs.append((path, linked))
    all_refs = sorted_refs([value[0] for value in refs.values()])
    identity = UUID(payload["source_id"]) if node.kind == "SourceRevision" else node.artifact_id
    operation_fingerprint = fingerprint(node, payload, all_refs, producer, identity)
    if existing is not None:
        if existing["operation_fingerprint"] != operation_fingerprint:
            raise Conflict("operation_id_reused_with_different_input")
        repo._verify_hash(tx, existing)
        return dict(existing)
    if payload.get("parent_script_ref") is not None:
        if ref.artifact_id != ArtifactRef.model_validate(payload["parent_script_ref"]).artifact_id:
            raise ContractError("parent_script_identity_mismatch")
    repo._validate_bindings(tx, project, node.kind, payload, refs, inputs=inputs, prepared=prepared)
    row = dict(
        project_id=project,
        revision_id=ref.revision_id,
        artifact_id=ref.artifact_id,
        kind=node.kind,
        episode_id=node.episode_id,
        schema_version="1.0",
        payload=payload,
        input_refs=[value.model_dump(mode="json") for value in all_refs],
        producer=producer,
        content_hash=content_hash(
            node.kind,
            "1.0",
            payload,
            all_refs,
            [dict(purpose=path, hash=blob.sha256) for path, blob in blobs],
        ),
        created_at=timestamp(),
        audience=node.audience,
        operation_id=node.operation_id,
        operation_fingerprint=operation_fingerprint,
        reference_policy=node.reference_policy,
    )
    groups["artifact_revisions"].append(row)
    for target, fields, owner in refs.values():
        if owner["project_id"] != project:
            publication = repo._publication(tx, project, owner)
            groups["catalog_references"].append(
                dict(
                    project_id=project,
                    consumer_id=ref.revision_id,
                    publication_id=publication["publication_id"],
                )
            )
            continue
        groups["revision_links"].append(
            dict(project_id=project, consumer_id=ref.revision_id, input_id=target.revision_id)
        )
        relations = {
            "lineage"
            if field in LINEAGE_FIELDS
            or node.kind == "SourceRevision"
            and target.artifact_id == ref.artifact_id
            and field == "input_refs"
            else "applies_policy"
            if target.kind in CONFIG_KINDS
            else "uses_content"
            for field in fields
        }
        for relation in sorted(relations):
            groups["artifact_dependencies"].append(
                dict(
                    project_id=project,
                    consumer_id=ref.revision_id,
                    input_id=target.revision_id,
                    relation=relation,
                    selector={},
                )
            )
        if fields & LINEAGE_FIELDS and node.kind in ("ScriptRevision", "ResearchNote", "StoryPlan"):
            for inherited in inputs.edges(target):
                if inherited["relation"] != "lineage":
                    groups["artifact_dependencies"].append(
                        dict(
                            project_id=project,
                            consumer_id=ref.revision_id,
                            input_id=inherited["input_id"],
                            relation=inherited["relation"],
                            selector={},
                        )
                    )
    for path, blob in blobs:
        groups["artifact_blobs"].append(
            dict(
                project_id=project,
                revision_id=ref.revision_id,
                storage_key=blob.storage_key,
                purpose=path,
            )
        )
    for runtime in dict.fromkeys(inputs.runtimes):
        groups["runtime_links"].append(
            dict(
                project_id=project,
                revision_id=ref.revision_id,
                record_id=runtime.record_id,
                record_kind=runtime.record_kind,
                read_required=runtime not in inputs.controls,
                privacy_required=True,
            )
        )
    groups["outbox"].append(
        dict(
            event_id=uuid4(),
            project_id=project,
            event_kind="artifact_created",
            payload=ref.model_dump(mode="json"),
        )
    )
    return row


def encoded(table, rows):
    columns = {
        "artifact_revisions": ("payload", "input_refs", "producer"),
        "artifact_dependencies": ("selector",),
        "outbox": ("payload",),
    }.get(table, ())
    return [dict(row, **{key: json_text(row[key]) for key in columns}) for row in rows], columns
