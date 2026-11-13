"""Permission checks never fetch/cache semantic artifact bodies or rescan loaded IDs."""

from collections import defaultdict
from io import BytesIO
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.access_facts import access_facts
from podgen.persistence.artifact_access import ArtifactFacts
from podgen.persistence.blob_access import BlobFacts
from podgen.persistence.read_authorization import _read_connection, read_operation
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.source_search_access import IndexAccess
from tests.authorization_helpers import withdraw_blob


class Key:
    hashes = 0

    def __init__(self, value):
        self.value = value

    def __hash__(self):
        type(self).hashes += 1
        return self.value


@pytest.mark.parametrize("width", [16, 4096])
def test_cached_lookup_work_depends_on_requested_keys_not_loaded_width(width):
    keys = [Key(value) for value in range(width)]
    artifacts = ArtifactFacts(None, None)
    artifacts.rows = dict.fromkeys(keys, {})
    blobs = BlobFacts(None, None, None)
    blobs.rows = dict.fromkeys((("p", key) for key in keys), {})
    Key.hashes = 0
    artifacts.load([keys[0]])
    artifact_work = Key.hashes
    Key.hashes = 0
    blobs.load("p", [keys[0]])
    assert artifact_work <= 4 and Key.hashes <= 4


def test_completed_closure_visits_are_linear_and_new_parents_propagate_denial():
    facts = ArtifactFacts(None, None)
    nodes = list(range(128))
    facts.rows = {node: dict(project_id="p") for node in nodes}
    facts.children = {node: {node + 1} if node < 127 else set() for node in nodes}
    facts.local_children = dict(facts.children)
    facts.parents = defaultdict(set)
    for parent, children in facts.children.items():
        for child in children:
            facts.parents[child].add(parent)
    visits = []
    original = facts.load

    def load(identities):
        visits.extend(identities)
        original(identities)

    facts.load = load
    for node in nodes:
        facts.expand([node])
    assert len(visits) == len(nodes)
    assert facts.expanded == set(nodes)
    facts._block([127])
    facts._propagate()
    facts.rows[128] = dict(project_id="p")
    facts.children[128] = facts.local_children[128] = {127}
    facts.parents[127].add(128)
    facts.expand([128])
    assert facts.blocked == set(range(129))


def test_authorization_has_no_payload_and_explicit_full_reads_retain_hash_and_body(repo, scope, producer):
    project, _ = scope
    text = "SYNTHETIC COMPLETE semantic artifact body " * 4000
    artifact = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(full=text)),
        producer=producer,
        operation_id=uuid4(),
    )
    root = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, root, dict(policy=artifact.ref.model_dump(mode="json")))
    full_reads = []

    def capture(conn, cursor, sql, parameters, context, many):
        if "AS access_graph" in sql:
            assert "payload" not in [column[0] for column in cursor.description]
        if "SELECT * FROM artifact_revisions" in sql:
            full_reads.append(sql)

    event.listen(repo.database.engine, "after_cursor_execute", capture)
    try:
        with read_operation(repo), _read_connection(repo) as tx:
            repo._runtime(tx, project, root)
            facts = access_facts(repo, tx)
            assert all("payload" not in row for row in facts.artifacts.rows.values())
            assert text not in repr(repo._read_authorization.get().completed)
            assert full_reads == []
            assert repo._resolve(tx, project, artifact.ref)["payload"] == artifact.payload
            assert repo._row(tx, artifact.ref)["payload"] == artifact.payload
            assert all("payload" not in row for row in facts.artifacts.rows.values())
        loaded = repo.get(project, artifact.ref)
        assert loaded.payload == artifact.payload and loaded.content_hash == artifact.content_hash
    finally:
        event.remove(repo.database.engine, "after_cursor_execute", capture)
    assert len(full_reads) == 3


def test_index_metadata_retains_artifact_runtime_blob_and_input_dependency_hashes(repo, scope, producer):
    project, _ = scope
    blobs = [
        repo.save_blob(project, BytesIO(f"SYNTHETIC dependency {i}".encode()), "text/plain") for i in range(3)
    ]
    child = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(blob=blobs[0].model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    runtime = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, runtime, dict(blob=blobs[1].model_dump(mode="json")))
    parent = repo.create(
        project,
        "Policy",
        dict(
            policy_kind="data",
            rules=dict(runtime=runtime.model_dump(mode="json"), blob=blobs[2].model_dump(mode="json")),
        ),
        input_refs=[child.ref],
        producer=producer,
        operation_id=uuid4(),
    )
    root = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, root, dict(policy=parent.ref.model_dump(mode="json")))
    access = IndexAccess(EvaluationJournal(repo, project), root)
    full_reads = []

    def capture(conn, cursor, sql, parameters, context, many):
        if "SELECT * FROM artifact_revisions" in sql:
            full_reads.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with access.transaction() as (_, hashes):
            assert hashes == {blob.sha256 for blob in blobs}
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert full_reads == []
    withdraw_blob(repo, project, blobs[0])
    with pytest.raises(AccessDenied), access.transaction():
        pytest.fail("withdrawn input dependency must deny index access")


@pytest.mark.parametrize("revocation", ["catalog_grant", "catalog_blob", "local_blob"])
def test_real_source_with_catalog_configuration_keeps_blob_owners_and_local_purge(
    repo, scope, producer, monkeypatch, revocation
):
    from podgen.domain.contracts.common import ModelProducer
    from podgen.persistence.catalog import ConfigRefResolver
    from podgen.persistence.database import query
    from podgen.retrieval.config import streaming_profile
    from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
    from podgen.writing.source_tools import SourceCorpus
    from tests.factories import Factory
    from tests.source_search_support import TinyModels
    from tests.test_frozen_source_tools import materials
    from tests.test_reusable_source_documents import search_args, table_count

    project, episode = scope
    catalog = uuid4()
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'catalog')", p=catalog)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'operator')", p=catalog, a=repo.actor_id)
    foreign = repo.save_blob(catalog, BytesIO(b"SYNTHETIC catalog configuration bytes"), "text/plain")
    policy = repo.create(
        catalog,
        "Policy",
        dict(policy_kind="data", rules=dict(blob=foreign.model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    resolver = ConfigRefResolver(repo)
    publication = resolver.publish(catalog, policy.ref, [project])
    factory = Factory(repo, project, episode, producer).setup()
    text = "Original bacteria source. " * 50
    source, local = factory.source(text)
    configured = repo.create(
        project,
        "SourceRevision",
        source.payload,
        input_refs=[policy.ref],
        artifact_id=source.ref.artifact_id,
        producer=producer,
        operation_id=uuid4(),
    )
    parent = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(source=configured.ref.model_dump(mode="json"))),
        producer=ModelProducer(kind="model", invocation_id=uuid4(), profile_ref=factory.model.ref),
        operation_id=uuid4(),
    )
    root = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, root, dict(input_ref=parent.ref.model_dump(mode="json")))
    journal = EvaluationJournal(repo, project)
    index = StreamingSearchIndex(journal, root, streaming_profile(), run_id=uuid4())
    monkeypatch.setattr("podgen.retrieval.streaming.models", lambda _: TinyModels())
    corpus = SourceCorpus(materials(text), retrieval=streaming_profile(), index=index)
    with index.access.transaction() as (_, hashes):
        assert local.sha256 in hashes and foreign.sha256 not in hashes
    assert corpus.retrieval.search(search_args())["hits"]
    assert table_count(repo, project, "source_search_documents") == 1
    if revocation == "catalog_grant":
        resolver.revoke(catalog, publication.publication_id, "SYNTHETIC revoke catalog grant")
    elif revocation == "catalog_blob":
        withdraw_blob(repo, catalog, foreign)
    else:
        withdraw_blob(repo, project, local)
        assert table_count(repo, project, "source_search_documents") == 0
        assert table_count(repo, project, "source_search_document_chunks") == 0
        assert table_count(repo, project, "source_search_results") == 0
    with pytest.raises(AccessDenied):
        corpus.retrieval.search(search_args())


def test_explicit_get_still_rejects_incorrect_artifact_content_hash(repo, scope, producer):
    from podgen.domain.contracts.common import ArtifactRef
    from podgen.domain.errors import ContractError
    from podgen.persistence.database import query

    project, _ = scope
    good = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(text="SYNTHETIC integrity")),
        producer=producer,
        operation_id=uuid4(),
    )
    bad = ArtifactRef(kind="Policy", artifact_id=good.ref.artifact_id, revision_id=uuid4())
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO artifact_revisions(project_id,revision_id,artifact_id,kind,episode_id,
            schema_version,payload,input_refs,producer,content_hash,created_at,audience,operation_id,operation_fingerprint)
            SELECT project_id,:new,artifact_id,kind,episode_id,schema_version,payload,input_refs,producer,
                'incorrect',created_at,audience,:op,operation_fingerprint FROM artifact_revisions WHERE revision_id=:old""",
            new=bad.revision_id,
            op=uuid4(),
            old=good.ref.revision_id,
        )
    with pytest.raises(ContractError, match="artifact_integrity_error"):
        repo.get(project, bad)
