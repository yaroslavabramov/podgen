"""Real SQL projection, exact historical forward semantics and atomic witness deduplication."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DataError, IntegrityError

from podgen.domain.references import walk_values
from podgen.persistence.classification import classify_evaluator
from podgen.persistence.database import json_text, one, query
from podgen.persistence.privacy_graph import protected_hashes
from tests.privacy_graph_cases import Graph, checksum, identity
from tests.privacy_projection_support import normalized_reference_payload, projection, seed_graph


def test_sql_projection_exact_shapes_full_unicode_and_old_insert_writer(repo, scope):
    project, _ = scope
    a, r = uuid4(), uuid4()
    artifact = dict(artifact_id=str(uuid4()), revision_id=str(a), kind="Policy")
    runtime = dict(record_id=str(r), record_kind="user_input")
    blob = dict(storage_key=f"{project}/blob", sha256=checksum(1), byte_length=7, media_type="text/plain")
    payload = dict(
        text="Full \0 original 🛰️",
        nested=[artifact, runtime, blob, artifact],
        diagnostic={**artifact, "extra": True},
        encoded=json.dumps(blob),
        nul_key={"\0": blob},
        escaped={"literal": r"\u0000", "slash_then_nul": "\\\0"},
    )
    record = uuid4()
    expected = sorted(
        [ref.model_dump(mode="json") for ref in {ref for _, _, ref in walk_values(payload)}], key=json_text
    )
    with repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=project,
            r=record,
            body=json_text(payload),
        )
        assert projection(tx, project, record) == expected
        assert one(
            tx,
            "SELECT payload::text AS raw FROM runtime_records WHERE project_id=:p AND record_id=:r",
            p=project,
            r=record,
        )["raw"] == json_text(payload)
        assert (
            one(
                tx,
                "SELECT payload FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=project,
                r=record,
            )["payload"]
            == payload
        )
        with pytest.raises(RuntimeError, match="rollback"):
            with tx.begin_nested():
                uncommitted = uuid4()
                query(
                    tx,
                    "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
                    p=project,
                    r=uncommitted,
                    body=json_text(payload),
                )
                assert projection(tx, project, uncommitted)
                raise RuntimeError("rollback")
        assert not projection(tx, project, uncommitted)


def test_old_insert_normalizes_contract_accepted_reference_scalars_without_rewriting_payload(repo, scope):
    project, _ = scope
    payload, record = normalized_reference_payload(project), uuid4()
    expected = sorted({ref for _, _, ref in walk_values(payload)}, key=lambda ref: json_text(ref))
    raw = json_text(payload)
    with repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=project,
            r=record,
            body=raw,
        )
        assert projection(tx, project, record) == [ref.model_dump(mode="json") for ref in expected]
        assert (
            one(
                tx,
                "SELECT payload::text AS raw FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=project,
                r=record,
            )["raw"].encode()
            == raw.encode()
        )

    for value in (4.5, "4.5", "4e0"):
        invalid = {**payload["blobs"][0], "byte_length": value}
        with pytest.raises(ValueError):
            list(walk_values(invalid))
        with pytest.raises(DataError), repo.database.transaction() as tx:
            query(
                tx,
                "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
                p=project,
                r=uuid4(),
                body=json_text({"blob": invalid}),
            )


def test_projection_preserves_literal_escapes_and_rejects_nul_inside_typed_ref(repo, scope):
    project, _ = scope
    for count in range(1, 7):
        # Literal backslashes followed by u0000 are valid metadata, not NUL.
        blob = dict(
            storage_key="prefix/" + "\\" * count + "u0000",
            sha256=checksum(count),
            byte_length=1,
            media_type="text/plain",
        )
        payload = {
            "text": "\\" * count + "\0",
            "ref": blob,
            "nested": {"key\0": {"same": blob}},
            "other": 'quoted " before literal \\u0000',
        }
        with repo.database.transaction() as tx:
            record = uuid4()
            query(
                tx,
                "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
                p=project,
                r=record,
                body=json_text(payload),
            )
            assert projection(tx, project, record) == [blob]
            assert (
                one(
                    tx,
                    "SELECT payload::text AS raw FROM runtime_records WHERE project_id=:p AND record_id=:r",
                    p=project,
                    r=record,
                )["raw"].encode()
                == json_text(payload).encode()
            )
    with repo.database.transaction() as tx:
        with pytest.raises(IntegrityError):
            with tx.begin_nested():
                record = uuid4()
                query(
                    tx,
                    "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
                    p=project,
                    r=record,
                    body=json_text({"blob": {**blob, "storage_key": "bad\0key"}}),
                )
        assert not projection(tx, project, record)


def test_late_alias_is_traversed_even_when_shared_hash_was_already_classified(repo, scope):
    project, _ = scope
    root, late, dependent = (identity(i) for i in range(1, 4))
    with repo.database.transaction() as tx:
        seed_graph(tx, project, Graph(runtime_blobs={(root, checksum(1))}))
        classify_evaluator(tx, project, runtime_id=root)
        # Publish a new alias after the initial classification. Its forward cone
        # must remain reachable; membership is not a traversal stop condition.
        blob = dict(
            storage_key=f"{project}/{checksum(1)}",
            sha256=checksum(1),
            byte_length=1,
            media_type="test/synthetic",
        )
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=project,
            r=late,
            body=json_text({"same_bytes": blob}),
        )
        query(
            tx,
            "INSERT INTO blobs VALUES(:p,:key,:sha,1,'test/synthetic')",
            p=project,
            key=f"{project}/{checksum(2)}",
            sha=checksum(2),
        )
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=project,
            r=dependent,
            body=json_text(
                {
                    "source": dict(record_id=str(late), record_kind="tool_execution"),
                    "derived_blob": {
                        **blob,
                        "storage_key": f"{project}/{checksum(2)}",
                        "sha256": checksum(2),
                    },
                }
            ),
        )
        classify_evaluator(tx, project, runtime_id=root)
        assert {
            r["sha256"]
            for r in query(
                tx, "SELECT sha256 FROM evaluator_blob_classifications WHERE project_id=:p", p=project
            ).mappings()
        } == {checksum(1), checksum(2)}


def test_indexed_closure_matches_oracle_through_all_edge_types_without_primary_input_taint(repo, scope):
    project, _ = scope
    identities = {i: uuid4() for i in range(1, 10)}
    node = identities.__getitem__
    primary, root, alias, derived, catalog = (node(i) for i in range(1, 6))
    graph = Graph(
        runtime_inputs={(root, primary), (derived, alias), (alias, derived)},
        runtime_blobs={
            (primary, checksum(1)),
            (root, checksum(2)),
            (alias, checksum(2)),
            (derived, checksum(3)),
        },
        artifact_runtimes={(catalog, derived)},
        catalog_blobs={(catalog, checksum(4))},
        artifact_blobs={(node(8), checksum(4)), (node(7), checksum(5))},
        original={(node(6), node(8))},
        revalidation={(node(7), node(6))},
        runtime_artifacts={(node(9), node(7))},
    )
    with repo.database.transaction() as tx:
        seed_graph(tx, project, graph)
        assert protected_hashes(tx, project, runtime_ids=[root]) == graph.legacy_closure(runtimes=[root])
        assert checksum(1) not in protected_hashes(tx, project, runtime_ids=[root])
        assert protected_hashes(tx, project, artifact_ids=[node(8)]) == graph.legacy_closure(
            artifacts=[node(8)]
        )
        assert protected_hashes(tx, uuid4(), runtime_ids=[root]) == set()


def test_repeated_shared_hash_classification_does_not_grow_witness_matrix(repo, scope):
    project, _ = scope
    root, another = identity(1), identity(2)
    graph = Graph(runtime_blobs={(root, checksum(1)), (another, checksum(1))})
    for number in range(3, 40):
        graph.runtime_inputs.add((identity(number), root))
        graph.runtime_blobs.add((identity(number), checksum(number)))
    # A large unrelated record is never selected by the closure.
    graph.runtime_blobs.add((identity(999), checksum(999)))
    statements = []
    with repo.database.transaction() as tx:
        seed_graph(tx, project, graph)
        event.listen(
            tx,
            "before_cursor_execute",
            lambda conn, cursor, sql, params, context, many: statements.append(sql),
        )
        classify_evaluator(tx, project, runtime_id=root)
        count = one(
            tx, "SELECT count(*) AS n FROM evaluator_blob_classifications WHERE project_id=:p", p=project
        )["n"]
        assert count == len(graph.legacy_closure(runtimes=[root]))
        classify_evaluator(tx, project, runtime_id=another)
        assert (
            one(
                tx, "SELECT count(*) AS n FROM evaluator_blob_classifications WHERE project_id=:p", p=project
            )["n"]
            == count
        )
        # Old code's INSERT also passes through the membership reservation trigger.
        query(
            tx,
            "INSERT INTO evaluator_blob_classifications VALUES(:p,:sha,NULL,:r) ON CONFLICT DO NOTHING",
            p=project,
            sha=checksum(1),
            r=another,
        )
        assert (
            one(
                tx, "SELECT count(*) AS n FROM evaluator_blob_classifications WHERE project_id=:p", p=project
            )["n"]
            == count
        )
    assert not any("SELECT record_id,payload FROM runtime_records" in sql for sql in statements)
    assert sum("INSERT INTO evaluator_blob_classifications" in sql for sql in statements) == 3


def test_failed_witness_rolls_back_membership_and_concurrent_writers_publish_one(repo, scope):
    project, _ = scope
    a, b = uuid4(), uuid4()
    with repo.database.transaction() as tx:
        seed_graph(tx, project, Graph(runtime_blobs={(a, checksum(1)), (b, checksum(1))}))
        with pytest.raises(IntegrityError):
            with tx.begin_nested():
                query(
                    tx,
                    "INSERT INTO evaluator_blob_classifications VALUES(:p,:sha,NULL,:r)",
                    p=project,
                    sha=checksum(2),
                    r=uuid4(),
                )
        assert (
            one(
                tx,
                "SELECT 1 FROM evaluator_protected_hashes WHERE project_id=:p AND sha256=:sha",
                p=project,
                sha=checksum(2),
            )
            is None
        )
    barrier = Barrier(2)

    def publish(root):
        with repo.database.transaction() as tx:
            barrier.wait(timeout=5)
            if root == a:
                classify_evaluator(tx, project, runtime_id=root)
            else:
                query(
                    tx,
                    "INSERT INTO evaluator_blob_classifications VALUES(:p,:sha,NULL,:r) ON CONFLICT DO NOTHING",
                    p=project,
                    sha=checksum(1),
                    r=root,
                )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(publish, [a, b]))
    with repo.database.transaction() as tx:
        assert (
            one(
                tx, "SELECT count(*) AS n FROM evaluator_blob_classifications WHERE project_id=:p", p=project
            )["n"]
            == 1
        )
        assert (
            one(tx, "SELECT count(*) AS n FROM evaluator_protected_hashes WHERE project_id=:p", p=project)[
                "n"
            ]
            == 1
        )


def test_migration_backfills_legacy_payloads_and_preserves_old_witnesses(database):
    # Independent schema exercises pre-migration rows and the actual SQL migration
    # without dropping or rewriting the shared test database's canonical tables.
    schema, project, record = "privacy_" + uuid4().hex, uuid4(), uuid4()
    blob = dict(storage_key=f"{project}/old", sha256=checksum(1), byte_length=4, media_type="text/plain")
    payload = {
        "full_text": "Legacy \0 text 🛰️",
        "original_blob": blob,
        "normalized": normalized_reference_payload(project),
    }
    expected = sorted(
        [ref.model_dump(mode="json") for ref in {ref for _, _, ref in walk_values(payload)}], key=json_text
    )
    sql = (
        Path(__file__).resolve().parents[1] / "migrations/versions/0025_runtime_reference_projection.sql"
    ).read_text()
    with database.transaction() as tx:
        query(tx, f"CREATE SCHEMA {schema}")
        try:
            query(tx, f"SET LOCAL search_path TO {schema},public")
            for table in (
                "projects",
                "runtime_records",
                "revision_links",
                "revalidation_privacy_links",
                "runtime_links",
                "artifact_blobs",
                "catalog_publications",
                "evaluator_blob_classifications",
            ):
                # CTAS avoids copying already installed projection indexes/triggers.
                query(tx, f"CREATE TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA")
            query(tx, "ALTER TABLE projects ADD PRIMARY KEY(project_id)")
            query(tx, "ALTER TABLE runtime_records ADD PRIMARY KEY(project_id,record_id)")
            query(
                tx,
                "INSERT INTO projects(project_id,namespace_kind,validity_version) VALUES(:p,'tenant',0)",
                p=project,
            )
            query(
                tx,
                "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
                p=project,
                r=record,
                body=json_text(payload),
            )
            # Two historical witnesses are retained, while future duplicates are suppressed.
            query(
                tx,
                "INSERT INTO evaluator_blob_classifications VALUES(:p,:sha,NULL,:r),(:p,:sha,NULL,:r2)",
                p=project,
                sha=checksum(1),
                r=record,
                r2=uuid4(),
            )
            tx.execute(text(sql))
            assert projection(tx, project, record) == expected
            assert (
                one(tx, "SELECT payload::text AS raw FROM runtime_records WHERE record_id=:r", r=record)[
                    "raw"
                ].encode()
                == json_text(payload).encode()
            )
            assert one(tx, "SELECT count(*) AS n FROM evaluator_blob_classifications")["n"] == 2
            assert one(tx, "SELECT count(*) AS n FROM evaluator_protected_hashes")["n"] == 1
            query(
                tx,
                "INSERT INTO evaluator_blob_classifications VALUES(:p,:sha,NULL,:r)",
                p=project,
                sha=checksum(1),
                r=uuid4(),
            )
            assert one(tx, "SELECT count(*) AS n FROM evaluator_blob_classifications")["n"] == 2
            assert (
                one(tx, "SELECT payload FROM runtime_records WHERE record_id=:r", r=record)["payload"]
                == payload
            )
        finally:
            query(tx, "SET LOCAL search_path TO public")
            query(tx, f"DROP SCHEMA {schema} CASCADE")
