"""Real project-local PostgreSQL/pgvector index with explicit cheap inference doubles."""

from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one, query
from podgen.persistence.repository import Repository
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.config import profile
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.source_search_index import PostgresSearchIndex
from podgen.writing.source_tools import SourceCorpus

from tests.source_search_support import TinyModels
from tests.test_frozen_source_tools import materials, search


def admitted(repo, project):
    journal = EvaluationJournal(repo, project)
    # Test fixture stands in for an admitted immutable input; the production FK/ACL checks are real.
    ref = journal.record(dict(contract="retrieval-test-input/1"))
    return journal, ref


def test_nonempty_cards_real_backend_rebuild_scope_and_no_cross_project_reuse(repo, scope, monkeypatch):
    project, _ = scope
    network = TinyModels()
    monkeypatch.setattr("podgen.retrieval.hybrid.models", lambda _: network)
    journal, ref = admitted(repo, project)
    index = PostgresSearchIndex(journal, ref, profile())
    data = materials("Old card. Researchers observed bacteria after raindrop impact.\n" * 30)
    corpus = SourceCorpus(data, retrieval=profile(), index=index)
    assert corpus.cards and corpus.retrieval.index is index
    first = search(corpus, "bacteria", "M1")
    assert first["hits"] and all(row["source_id"] == "S1" for row in first["hits"])
    emitted = network.embedded
    reopened = SourceCorpus(data, retrieval=profile(), index=PostgresSearchIndex(journal, ref, profile()))
    assert network.embedded == emitted
    assert search(reopened, "bacteria", "S1") == first
    with repo.database.transaction() as tx:
        query(
            tx,
            "DELETE FROM source_search_chunks WHERE project_id=:p AND start_offset=(SELECT min(start_offset) FROM source_search_chunks WHERE project_id=:p)",
            p=project,
        )
    SourceCorpus(data, retrieval=profile(), index=PostgresSearchIndex(journal, ref, profile()))
    assert network.embedded > emitted
    other = uuid4()
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=other)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=other, a=repo.actor_id)
    other_journal, other_ref = admitted(repo, other)
    before = network.embedded
    SourceCorpus(data, retrieval=profile(), index=PostgresSearchIndex(other_journal, other_ref, profile()))
    assert network.embedded > before
    stranger = Repository(repo.database, repo.objects, uuid4(), purpose="evaluation")
    forbidden = PostgresSearchIndex(EvaluationJournal(stranger, project), ref, profile())
    with pytest.raises(AccessDenied):
        forbidden.candidates("bacteria", network.query("q"), {"S1"}, 6)


def test_scope_filters_all_branches_before_ranking_and_fts_handles_language(repo, scope):
    project, _ = scope
    journal, ref = admitted(repo, project)
    index = PostgresSearchIndex(journal, ref, profile())
    vector = np.zeros((1, 384), dtype=np.float32)
    vector[0, 0] = 1
    for source_id, text, language in (
        ("S1", "Bacteria survived on 25 November 1854. Henry Whitehead.", "en"),
        ("S2", "Бактерии остались живыми. Ноябрь 1854.", "ru"),
    ):
        doc = SimpleNamespace(text=text, digest=source_id * 32)
        index.add_document(source_id, doc, [(0, len(text))], language, vector)
    for scope_ids in ({"S1"}, {"S2"}, set()):
        branches = index.candidates("Henry Whitehead 1854 бактериями", vector[0], scope_ids, 6)
        assert all(chunk.source_id in scope_ids for branch in branches.values() for chunk, _ in branch)
    assert index.candidates("бактериями", vector[0], {"S2"}, 6)["lexical"]
    assert index.candidates("surviving", vector[0], {"S1"}, 6)["lexical"]
    assert index.candidates("Henry Whitehead", vector[0], {"S1"}, 6)["exact"]


def test_failed_publication_is_atomic_and_project_delete_removes_projection(repo, scope, monkeypatch):
    project, _ = scope
    journal, ref = admitted(repo, project)
    index = PostgresSearchIndex(journal, ref, profile())
    doc = SimpleNamespace(text="First passage. Second passage.", digest="a" * 64)
    vectors = np.zeros((2, 384), dtype=np.float32)
    vectors[:, 0] = 1
    from podgen.runtime.evaluation import source_search_index as module

    original = module.query

    def fail(tx, statement, **values):
        if statement.startswith("INSERT INTO source_search_chunks") and values["start"] > 0:
            from sqlalchemy.exc import OperationalError

            raise OperationalError("injected", {}, Exception("interrupted write"))
        return original(tx, statement, **values)

    monkeypatch.setattr(module, "query", fail)
    with pytest.raises(RetrievalUnavailable):
        index.add_document("S1", doc, [(0, 14), (15, len(doc.text))], "en", vectors)
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM source_search_indexes WHERE project_id=:p", p=project)["n"]
            == 0
        )
    monkeypatch.setattr(module, "query", original)
    index.add_document("S1", doc, [(0, 14), (15, len(doc.text))], "en", vectors)
    # Deleting the rebuildable projection cascades its private FTS/vector rows.
    with repo.database.transaction() as tx:
        query(tx, "DELETE FROM source_search_indexes WHERE project_id=:p", p=project)
        assert (
            one(tx, "SELECT count(*) AS n FROM source_search_chunks WHERE project_id=:p", p=project)["n"] == 0
        )


def test_blob_withdrawal_deletes_vectors_and_blocks_cached_results_and_rebuild(
    repo, scope, producer, monkeypatch
):
    from podgen.persistence.dependencies import DependencyLedger

    from tests.factories import Factory

    factory = Factory(repo, *scope, producer).setup()
    raw = "Old card. A private experiment observed living bacteria."
    source, _ = factory.source(raw)
    journal = EvaluationJournal(repo, factory.project)
    ref = journal.record(dict(source_ref=source.ref.model_dump(mode="json")))
    network = TinyModels()
    monkeypatch.setattr("podgen.retrieval.hybrid.models", lambda _: network)
    corpus = SourceCorpus(
        materials(raw), retrieval=profile(), index=PostgresSearchIndex(journal, ref, profile())
    )
    assert search(corpus, "bacteria")["hits"]
    DependencyLedger(repo).invalidate(
        factory.project, source.ref, reason="withdraw test source", category="access"
    )
    with repo.database.transaction() as tx:
        for table in ("source_search_indexes", "source_search_chunks", "source_search_dependencies"):
            assert (
                one(tx, f"SELECT count(*) AS n FROM {table} WHERE project_id=:p", p=factory.project)["n"] == 0
            )
    with pytest.raises(AccessDenied):
        search(corpus, "bacteria")
    with pytest.raises(AccessDenied):
        corpus.execute("read_source", dict(source_id="S1", start=0, end=9), read_id="E1")
    with pytest.raises(AccessDenied):
        SourceCorpus(materials(raw), retrieval=profile(), index=PostgresSearchIndex(journal, ref, profile()))


def test_withdrawal_wins_while_embedding_and_stale_builder_cannot_republish(
    repo, scope, producer, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from podgen.persistence.dependencies import DependencyLedger

    from tests.factories import Factory

    factory = Factory(repo, *scope, producer).setup()
    raw = "Old card. Pending source indexing must respect content withdrawal."
    source, _ = factory.source(raw)
    journal = EvaluationJournal(repo, factory.project)
    ref = journal.record(dict(source_ref=source.ref.model_dump(mode="json")))
    entered, release = Event(), Event()

    class WaitingModels(TinyModels):
        def embed(self, texts):
            entered.set()
            assert release.wait(5)
            return super().embed(texts)

    monkeypatch.setattr("podgen.retrieval.hybrid.models", lambda _: WaitingModels())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            SourceCorpus,
            materials(raw),
            retrieval=profile(),
            index=PostgresSearchIndex(journal, ref, profile()),
        )
        assert entered.wait(5)
        try:
            DependencyLedger(repo).invalidate(
                factory.project, source.ref, reason="withdraw during inference", category="access"
            )
        finally:
            release.set()
        with pytest.raises(AccessDenied):
            future.result(timeout=5)
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM source_search_chunks WHERE project_id=:p", p=factory.project)[
                "n"
            ]
            == 0
        )
