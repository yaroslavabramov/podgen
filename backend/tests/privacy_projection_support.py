"""Synthetic graph publication and exact projection inspection for PostgreSQL tests."""

from uuid import uuid4

from podgen.persistence.database import json_text, query
from tests.privacy_graph_cases import checksum


def seed_graph(tx, project, graph):
    artifacts = set()
    runtimes = set()
    for consumer, source in graph.original | graph.revalidation:
        artifacts.update((consumer, source))
    for runtime, source in graph.runtime_artifacts:
        runtimes.add(runtime)
        artifacts.add(source)
    for runtime, source in graph.runtime_inputs:
        runtimes.update((runtime, source))
    for artifact, runtime in graph.artifact_runtimes:
        artifacts.add(artifact)
        runtimes.add(runtime)
    artifacts.update(a for a, _ in graph.artifact_blobs | graph.catalog_blobs)
    runtimes.update(r for r, _ in graph.runtime_blobs)
    blobs = {sha for _, sha in graph.runtime_blobs | graph.artifact_blobs | graph.catalog_blobs}
    for artifact in artifacts:
        query(tx, "INSERT INTO artifacts VALUES(:p,:a,'DiagnosticArtifact')", p=project, a=artifact)
        query(
            tx,
            """INSERT INTO artifact_revisions VALUES(
              :p,:a,:a,'DiagnosticArtifact',NULL,'1.0','{}'::json,'[]'::jsonb,'{}'::jsonb,
              :sha,now(),'project',:op,'synthetic')""",
            p=project,
            a=artifact,
            sha=checksum(0),
            op=uuid4(),
        )
    for sha in blobs:
        query(
            tx,
            "INSERT INTO blobs VALUES(:p,:k,:sha,1,'test/synthetic')",
            p=project,
            k=f"{project}/{sha}",
            sha=sha,
        )
    for table, edges in [
        ("revision_links", graph.original),
        ("revalidation_privacy_links", graph.revalidation),
    ]:
        for consumer, source in edges:
            query(tx, f"INSERT INTO {table} VALUES(:p,:c,:i)", p=project, c=consumer, i=source)
    for runtime in runtimes:
        payload = dict(
            artifacts=[
                dict(artifact_id=str(a), revision_id=str(a), kind="DiagnosticArtifact")
                for r, a in graph.runtime_artifacts
                if r == runtime
            ],
            inputs=[
                dict(record_id=str(source), record_kind="tool_execution")
                for r, source in graph.runtime_inputs
                if r == runtime
            ],
            blobs=[
                dict(storage_key=f"{project}/{sha}", sha256=sha, byte_length=1, media_type="test/synthetic")
                for r, sha in graph.runtime_blobs
                if r == runtime
            ],
            full_text="SYNTHETIC original text \0 with Unicode 🛰️ retained verbatim.",
        )
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=project,
            r=runtime,
            body=json_text(payload),
        )
    for artifact, runtime in graph.artifact_runtimes:
        query(
            tx,
            "INSERT INTO runtime_links VALUES(:p,:a,:r,'tool_execution')",
            p=project,
            a=artifact,
            r=runtime,
        )
    for artifact, sha in graph.artifact_blobs:
        query(
            tx,
            "INSERT INTO artifact_blobs VALUES(:p,:a,:k,:purpose)",
            p=project,
            a=artifact,
            k=f"{project}/{sha}",
            purpose=sha,
        )
    for artifact in {a for a, _ in graph.catalog_blobs}:
        config = dict(
            published_assets=[
                dict(
                    blob_ref=dict(
                        storage_key=f"{project}/{sha}", sha256=sha, byte_length=1, media_type="test/synthetic"
                    )
                )
                for a, sha in graph.catalog_blobs
                if a == artifact
            ]
        )
        query(
            tx,
            "INSERT INTO catalog_publications VALUES(:id,:p,:a,CAST(:config AS jsonb),:who,now())",
            id=uuid4(),
            p=project,
            a=artifact,
            config=json_text(config),
            who=uuid4(),
        )


def projection(tx, project, record):
    result = []
    for table, fields in [
        ("runtime_artifact_refs", "artifact_id,revision_id,kind"),
        ("runtime_runtime_refs", "target_record_id,record_kind"),
        ("runtime_blob_refs", "storage_key,sha256,byte_length,media_type"),
    ]:
        for row in query(
            tx, f"SELECT {fields} FROM {table} WHERE project_id=:p AND record_id=:r", p=project, r=record
        ).mappings():
            ref = dict(row)
            if "target_record_id" in ref:
                ref["record_id"] = ref.pop("target_record_id")
            result.append({key: str(value) if key.endswith("_id") else value for key, value in ref.items()})
    return sorted(result, key=json_text)


def normalized_reference_payload(project):
    """Raw JSON accepted by walk_values, before its Pydantic scalar coercions."""
    a, r = uuid4(), uuid4()
    payload = {
        "raw": "Original \0 text",
        "artifacts": [dict(artifact_id=a.urn, revision_id=r.urn, kind="Policy")],
        "runtimes": [
            dict(record_id=value, record_kind="tool_execution")
            for value in (r.urn, str(r).upper(), r.hex, "{" + str(r) + "}")
        ],
        "blobs": [
            dict(
                storage_key=f"{project}/raw-{index}",
                sha256=checksum(index),
                byte_length=value,
                media_type="text/plain",
            )
            for index, value in enumerate(
                (
                    4,
                    4.0,
                    "4.0",
                    "004.00",
                    "+4.0",
                    "4_0",
                    "1_000.0",
                    "\u20034.0\u00a0",
                    True,
                    False,
                    "-0.0",
                    1e18,
                    1.2345678901234568e18,
                    9223372036854775807,
                    "9223372036854775807",
                )
            )
        ],
    }
    return payload
