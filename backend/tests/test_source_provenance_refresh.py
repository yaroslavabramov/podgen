"""Identical source bytes still bind newly admitted provenance for withdrawal."""

from io import BytesIO
from uuid import uuid4

import pytest

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.retrieval.config import streaming_profile
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from podgen.sources.material_requests import SourceAdmission
from podgen.writing.source_tools import SourceCorpus
from tests.authorization_helpers import withdraw_blob
from tests.source_search_support import TinyModels
from tests.test_frozen_source_tools import materials
from tests.test_reusable_source_documents import search_args, table_count
from tests.test_source_search_index import admitted


def test_same_source_new_admission_binds_dependencies_without_reindexing(repo, scope, monkeypatch):
    project, _ = scope
    journal, reference = admitted(repo, project)
    network = TinyModels()
    monkeypatch.setattr("podgen.retrieval.streaming.models", lambda _: network)
    index = StreamingSearchIndex(journal, reference, streaming_profile(), run_id=uuid4())
    corpus = SourceCorpus(
        materials("Original bacteria source. " * 50), retrieval=streaming_profile(), index=index
    )
    doc = corpus.documents["S1"]
    corpus.admit(SourceAdmission("S1", doc.title, doc.text, doc.digest, reference))
    corpus.refresh()
    assert corpus.retrieval.search(search_args())["hits"]
    assert table_count(repo, project, "source_search_results") == 1
    emitted, revision = network.embedded, corpus.retrieval.revision

    dependency = repo.save_blob(project, BytesIO(b"SYNTHETIC additional provenance only"), "text/plain")
    provenance = journal.record(dict(dependency=dependency.model_dump(mode="json")))
    corpus.admit(SourceAdmission("S1", doc.title, doc.text, doc.digest, provenance))
    assert dependency.sha256 not in index.registered_dependencies

    def registered_count():
        with repo.database.transaction() as tx:
            return one(
                tx,
                "SELECT count(*) AS n FROM source_search_scope_dependencies WHERE project_id=:p AND shared_input_record_id=:r AND sha256=:sha",
                p=project,
                r=index.params["r"],
                sha=dependency.sha256,
            )["n"]

    from podgen.runtime.evaluation import streaming_search_index

    register = streaming_search_index.register_dependencies

    def fail_after_insert(*args):
        register(*args)
        raise RuntimeError("rollback new provenance")

    with monkeypatch.context() as patch:
        patch.setattr(streaming_search_index, "register_dependencies", fail_after_insert)
        with pytest.raises(RuntimeError, match="rollback new provenance"):
            corpus.refresh()
    assert dependency.sha256 not in index.registered_dependencies
    assert registered_count() == 0

    corpus.refresh()
    assert registered_count() == 1
    assert dependency.sha256 in index.registered_dependencies
    assert corpus.retrieval.revision == revision and network.embedded == emitted
    assert table_count(repo, project, "source_search_results") == 1

    withdraw_blob(repo, project, dependency)
    assert table_count(repo, project, "source_search_results") == 0
    assert table_count(repo, project, "source_search_documents") == 0
    assert table_count(repo, project, "source_search_document_chunks") == 0
    with pytest.raises(AccessDenied):
        corpus.retrieval.search(search_args())
