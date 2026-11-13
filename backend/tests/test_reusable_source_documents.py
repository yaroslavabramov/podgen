"""Real SQL cache reuse and run-local authorization; inference is an explicitly counted double."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from podgen.domain.errors import AccessDenied, Conflict
from podgen.persistence.database import one, query
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.config import streaming_profile
from podgen.retrieval.streaming import StreamingSearch
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from tests.source_search_support import TinyModels
from tests.test_source_search_index import admitted
from tests.test_streaming_source_search import CharacterModels


def document(text):
    return SimpleNamespace(title="Retained original", text=text, digest=sha256(text.encode()).hexdigest())


def build(journal, ref, documents, network, *, run_id=None, profile=None):
    profile = profile or streaming_profile()
    index = StreamingSearchIndex(journal, ref, profile, run_id=run_id or uuid4())
    return StreamingSearch(documents, {}, profile, index=index, network=network)


def search_args(query="bacteria", **changes):
    return SimpleNamespace(query=query, within=None, offset=0, result_version=None, **changes)


def table_count(repo, project, table):
    with repo.database.transaction() as tx:
        return one(tx, f"SELECT count(*) AS n FROM {table} WHERE project_id=:p", p=project)["n"]


def test_second_run_reuses_document_vectors_but_not_local_source_labels(repo, scope):
    journal, ref = admitted(repo, scope[0])
    network = TinyModels()
    doc = document("Bacteria source exact. " * 500)
    first = build(journal, ref, {"S63": doc}, network)
    emitted = network.embedded
    chunks = table_count(repo, scope[0], "source_search_document_chunks")
    second = build(journal, ref, {"S7": doc}, network)
    assert network.embedded == emitted > 0
    assert table_count(repo, scope[0], "source_search_documents") == 1
    assert table_count(repo, scope[0], "source_search_document_chunks") == chunks
    assert table_count(repo, scope[0], "source_search_memberships") == 2
    assert first.index.params["r"] != second.index.params["r"]
    assert all(hit["source_id"] == "S7" for hit in second.search(search_args())["hits"])
    assert not any(second.index.candidates("bacteria", network.query("q"), {"S63"}, 6).values())
    # A separate admitted input in the same project can also reuse exact content.
    _, other_ref = admitted(repo, scope[0])
    emitted = network.embedded
    build(journal, other_ref, {"S9": doc}, network)
    assert network.embedded == emitted


def test_identical_added_ids_in_two_runs_never_alias_different_documents(repo, scope):
    journal, ref = admitted(repo, scope[0])
    network = TinyModels()
    first_run = uuid4()
    first = build(journal, ref, {"S63": document("Alpha-only experiment.")}, network, run_id=first_run)
    second = build(journal, ref, {"S63": document("Beta-only independently retained.")}, network)
    assert table_count(repo, scope[0], "source_search_documents") == 2
    for current, word, absent in ((first, "Alpha", "Beta"), (second, "Beta", "Alpha")):
        hits = current.search(search_args(word))["hits"]
        assert hits and all(word in h["preview"] and absent not in h["preview"] for h in hits)
    with pytest.raises(Conflict, match="document_binding_changed"):
        build(journal, ref, {"S63": document("Changed in-place")}, network, run_id=first_run)


@pytest.mark.parametrize("setting", ["chunking", "weights", "tokenizer", "pooling", "prefix", "text"])
def test_changed_processing_identity_requires_fresh_document_computation(repo, scope, monkeypatch, setting):
    from podgen.retrieval import config

    journal, ref = admitted(repo, scope[0])
    profile = streaming_profile()
    network = TinyModels()
    doc = document("Bacteria input with exact Unicode ёжик 😀. " * 100)
    build(journal, ref, {"S1": doc}, network)
    emitted = network.embedded
    updated = deepcopy(profile)
    if setting == "chunking":
        updated["chunk_tokens"] = 112
    elif setting == "weights":
        updated["encoder"]["files"]["onnx/model_quantized.onnx"] = "a" * 64
    elif setting == "tokenizer":
        updated["encoder"]["files"]["tokenizer.json"] = "b" * 64
    elif setting == "pooling":
        updated["pooling"] = "test-new-pooling/2"
    elif setting == "prefix":
        updated["prefixes"]["passage"] = "test new passage: "
    else:
        doc = document(doc.text + "Changed original text.")
    # Simulate the next published profile with an explicit inference double.
    monkeypatch.setattr(config, "STREAMING_PROFILE", updated)
    build(journal, ref, {"S1": doc}, network, profile=updated)
    assert network.embedded > emitted
    assert table_count(repo, scope[0], "source_search_documents") == 2


def test_interrupted_document_resumes_across_runs_without_reembedding_committed_batches(repo, scope):
    journal, ref = admitted(repo, scope[0])
    doc = document("Exact bacteria record. " * 1000)
    profile, network = streaming_profile(), CharacterModels()
    first = StreamingSearchIndex(journal, ref, profile, run_id=uuid4())
    network.fail_at = 2
    with pytest.raises(RuntimeError, match="interrupted inference"):
        StreamingSearch({"S1": doc}, {}, profile, index=first, network=network)
    assert not any(first.candidates("bacteria", np.eye(1, 384, dtype=np.float32)[0], {"S1"}, 6).values())
    committed = table_count(repo, scope[0], "source_search_document_chunks")
    assert committed > 0
    network.fail_at = None
    resumed = build(journal, ref, {"S77": doc}, network)
    total = table_count(repo, scope[0], "source_search_document_chunks")
    assert network.embedded == total  # each document chunk embedded once, including before interruption
    assert resumed.search(search_args())["hits"]
    assert table_count(repo, scope[0], "source_search_documents") == 1
    assert first.candidates("bacteria", network.query("q"), {"S1"}, 6)["dense"]


def test_competing_builder_is_explicitly_busy_without_duplicate_inference(repo, scope):
    journal, ref = admitted(repo, scope[0])
    doc = document("Bacteria retained source.")
    a = StreamingSearchIndex(journal, ref, streaming_profile(), run_id=uuid4())
    b = StreamingSearchIndex(journal, ref, streaming_profile(), run_id=uuid4())
    with a.document("S1", doc, "en"):
        with pytest.raises(RetrievalUnavailable, match="document_build_busy"):
            with b.document("S2", doc, "en"):
                pytest.fail("concurrent inference must not be permitted")
    build(journal, ref, {"S2": doc}, TinyModels())


def test_persisted_pages_survive_corpus_growth_and_document_reuse(repo, scope):
    journal, ref = admitted(repo, scope[0])
    doc = document("Exact bacteria source. " * 1000)
    network = CharacterModels()
    run = uuid4()
    first = build(journal, ref, {"S1": doc}, network, run_id=run)
    page = first.search(search_args())
    assert page["next_offset"] is not None
    args = SimpleNamespace(
        query="bacteria", within=None, offset=page["next_offset"], result_version=page["result_version"]
    )
    next_page = first.search(args)
    reopened = build(
        journal, ref, {"S1": doc, "S2": document("New bacteria discovery.")}, network, run_id=run
    )
    emitted = network.embedded
    assert reopened.search(args) == next_page
    assert network.embedded == emitted  # stable version neither re-embeds query nor reranks
    other = build(journal, ref, {"S99": doc}, network)
    with pytest.raises(Conflict, match="result_version_mismatch"):
        other.search(args)


def test_withdrawal_purges_shared_chunks_and_blocks_every_run_and_stale_builder(repo, scope, producer):
    from podgen.persistence.dependencies import DependencyLedger
    from tests.factories import Factory

    factory = Factory(repo, *scope, producer).setup()
    doc = document("Private retained bacteria source.")
    source, _ = factory.source(doc.text)
    journal, _ = admitted(repo, scope[0])
    ref = journal.record(dict(source_ref=source.ref.model_dump(mode="json")))
    network = TinyModels()
    first = build(journal, ref, {"S1": doc}, network)
    second = build(journal, ref, {"S4": doc}, network)
    saved = second.search(search_args())
    DependencyLedger(repo).invalidate(scope[0], source.ref, reason="withdraw original", category="access")
    for table in (
        "source_search_documents",
        "source_search_document_chunks",
        "source_search_memberships",
        "source_search_results",
    ):
        assert table_count(repo, scope[0], table) == 0
    for current in (first, second):
        with pytest.raises(AccessDenied):
            current.search(
                SimpleNamespace(
                    query="bacteria", within=None, offset=0, result_version=saved["result_version"]
                )
            )
    with pytest.raises(AccessDenied):
        build(journal, ref, {"S1": doc}, network)
    with pytest.raises(AccessDenied):
        first.index.append_batch("S1", doc, [], "en", np.empty((0, 384)), previous=0, cursor=len(doc.text))


def test_membership_withdrawal_blocks_cached_pages_without_deleting_shared_document(repo, scope):
    journal, ref = admitted(repo, scope[0])
    network = TinyModels()
    current = build(journal, ref, {"S1": document("Bacteria source.")}, network)
    current.search(search_args())
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=scope[0], a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        current.search(search_args())


def test_reusable_document_data_is_never_shared_between_projects(repo, scope):
    journal, ref = admitted(repo, scope[0])
    network, doc = TinyModels(), document("Identical original bacteria source.")
    build(journal, ref, {"S1": doc}, network)
    before = network.embedded
    project = uuid4()
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)
    other, other_ref = admitted(repo, project)
    build(other, other_ref, {"S1": doc}, network)
    assert network.embedded > before


@pytest.mark.parametrize("completed", [True, False])
def test_existing_v2_projection_is_adopted_with_exact_cursor_and_without_reembedding(repo, scope, completed):
    from podgen.retrieval.lexical import language
    from podgen.retrieval.streaming_chunks import batches
    from podgen.runtime.evaluation.source_search_index import PostgresSearchIndex

    journal, ref = admitted(repo, scope[0])
    profile, network = streaming_profile(), CharacterModels()
    doc = document("Legacy bacteria record. " * 1000)
    run = uuid4()
    index = StreamingSearchIndex(journal, ref, profile, run_id=run)
    legacy = PostgresSearchIndex(journal, index.access.reference, profile)
    prepared = list(batches(doc.text, network, profile))
    old_batches = prepared if completed else prepared[:1]
    ranges = [pair for selected, _ in old_batches for pair in selected]
    vectors = network.embed([doc.text[a:b] for a, b in ranges])
    legacy.add_document("S1", doc, ranges, language(doc.text[:16000]), vectors)
    with repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO source_search_builds VALUES(:p,:r,:h,'S1',:cursor,:done)",
            **legacy.params,
            cursor=old_batches[-1][1],
            done=completed,
        )
    old_count = len(ranges)
    emitted = network.embedded
    reopened = StreamingSearch({"S1": doc}, {}, profile, index=index, network=network)
    total = table_count(repo, scope[0], "source_search_document_chunks")
    assert network.embedded == total
    if completed:
        assert network.embedded == emitted == total
    else:
        assert total > old_count
    assert table_count(repo, scope[0], "source_search_chunks") == old_count
    assert reopened.search(search_args())["hits"]
    before = network.embedded
    build(journal, ref, {"S4": doc}, network)
    assert network.embedded == before
