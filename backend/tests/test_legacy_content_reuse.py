"""Completed legacy computation is reusable across run scopes without borrowing access."""

import pytest

from podgen.domain.errors import Conflict
from podgen.persistence.database import query
from podgen.retrieval.config import streaming_profile
from podgen.retrieval.lexical import language
from podgen.runtime.evaluation.source_search_index import PostgresSearchIndex
from tests.source_search_support import TinyModels
from tests.test_reusable_source_documents import build, document, table_count
from tests.test_source_search_index import admitted


def legacy(repo, project, *, completed=True, corrupt=None):
    journal, reference = admitted(repo, project)
    doc = document("Exact legacy bacteria record. " * 50)
    network, profile = TinyModels(), streaming_profile()
    index = PostgresSearchIndex(journal, reference, profile)
    ranges = [(0, len(doc.text))]
    index.add_document("S63", doc, ranges, language(doc.text[:16000]), network.embed([doc.text]))
    with repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO source_search_builds VALUES(:p,:r,:h,'S63',:cursor,:done)",
            **index.params,
            cursor=len(doc.text) if completed else 500,
            done=completed,
        )
        if corrupt == "count":
            query(
                tx,
                "UPDATE source_search_indexes SET chunk_count=2 WHERE project_id=:p AND shared_input_record_id=:r",
                p=project,
                r=reference.record_id,
            )
        elif corrupt == "language":
            query(
                tx,
                "UPDATE source_search_chunks SET language='ru' WHERE project_id=:p AND shared_input_record_id=:r",
                p=project,
                r=reference.record_id,
            )
    return journal, reference, doc


def test_complete_legacy_build_is_adopted_across_scope_and_source_label(repo, scope):
    journal, old_ref, doc = legacy(repo, scope[0])
    _, new_ref = admitted(repo, scope[0])
    network = TinyModels()
    result = build(journal, new_ref, {"S7": doc}, network)
    assert network.embedded == 0
    assert table_count(repo, scope[0], "source_search_document_chunks") == 1
    assert result.index.params["r"] != old_ref.record_id
    again = build(journal, new_ref, {"S9": doc}, network)
    assert network.embedded == 0
    assert set(again.documents) == {"S9"}


def test_incomplete_legacy_from_another_scope_is_never_adopted_as_complete(repo, scope):
    journal, _, doc = legacy(repo, scope[0], completed=False)
    _, new_ref = admitted(repo, scope[0])
    network = TinyModels()
    build(journal, new_ref, {"S7": doc}, network)
    assert network.embedded > 0


@pytest.mark.parametrize("corrupt", ["count", "language"])
def test_corrupt_completed_legacy_is_not_published(repo, scope, corrupt):
    journal, _, doc = legacy(repo, scope[0], corrupt=corrupt)
    _, new_ref = admitted(repo, scope[0])
    network = TinyModels()
    with pytest.raises(Conflict, match="legacy_chunk_count_mismatch"):
        build(journal, new_ref, {"S7": doc}, network)
    assert network.embedded == 0
    assert table_count(repo, scope[0], "source_search_documents") == 0
    assert table_count(repo, scope[0], "source_search_document_chunks") == 0
