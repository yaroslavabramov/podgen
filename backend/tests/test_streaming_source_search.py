"""Cross old corpus ceilings with bounded batches; qualify the real durable SQL adapter."""

from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from podgen.persistence.database import one
from podgen.retrieval.config import streaming_profile
from podgen.retrieval.streaming import StreamingSearch
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from tests.source_search_support import TinyModels
from tests.test_source_search_index import admitted


class CharacterModels(TinyModels):
    largest_window = 0
    largest_batch = 0
    fail_at = None
    batches = 0

    def offsets(self, text):
        self.largest_window = max(self.largest_window, len(text))
        return [(i, i + 1) for i in range(len(text))]

    def embed(self, texts):
        self.batches += 1
        if self.batches == self.fail_at:
            raise RuntimeError("interrupted inference")
        self.largest_batch = max(self.largest_batch, len(texts))
        return super().embed(texts)


class VirtualText:
    def __init__(self, length):
        self.length = length

    def __len__(self):
        return self.length

    def __getitem__(self, key):
        start, stop, step = key.indices(self.length)
        assert stop - start <= 16384  # no full-source tokenization/read
        return "a" * (stop - start)


class CountingIndex:
    def __init__(self):
        self.states, self.count = {}, 0

    def begin_document(self, source, doc, code):
        return self.states.setdefault(source, dict(completed=False, cursor_position=0))

    def append_batch(self, source, doc, ranges, code, vectors, *, previous, cursor):
        state = self.states[source]
        assert state["cursor_position"] == previous
        self.count += len(ranges)
        state.update(cursor_position=cursor, completed=cursor == len(doc.text))

    def finish_empty(self, source):
        self.states[source]["completed"] = True


def test_corpus_above_all_three_old_limits_never_materializes_global_chunks_or_vectors():
    docs = {
        f"S{i}": SimpleNamespace(title="Exact document", text=VirtualText(10), digest=f"{i:064x}")
        for i in range(1, 71)
    }
    docs["S71"] = SimpleNamespace(title="Large exact source", text=VirtualText(2500001), digest="f" * 64)
    index, network = CountingIndex(), CharacterModels()
    search = StreamingSearch(docs, {}, streaming_profile(), index=index, network=network)
    assert len(docs) > 64 and sum(len(doc.text) for doc in docs.values()) > 2000000 and index.count > 12000
    assert network.largest_window <= 16384 and network.largest_batch <= 32
    assert not hasattr(search, "chunks") and all(state["completed"] for state in index.states.values())
    emitted = network.embedded
    search.refresh()
    assert network.embedded == emitted


def test_real_sql_batch_resume_publishes_only_complete_documents_and_keeps_result_pages(repo, scope):
    journal, ref = admitted(repo, scope[0])
    run = uuid4()
    index = StreamingSearchIndex(journal, ref, streaming_profile(), run_id=run)
    text = "Exact bacteria source. " * 1000
    doc = SimpleNamespace(title="Record", text=text, digest="a" * 64)
    network = CharacterModels()
    network.fail_at = 2
    with pytest.raises(RuntimeError, match="interrupted inference"):
        StreamingSearch({"S1": doc}, {}, streaming_profile(), index=index, network=network)
    assert not any(index.candidates("bacteria", np.eye(1, 384, dtype=np.float32)[0], {"S1"}, 6).values())
    network.fail_at = None
    search = StreamingSearch({"S1": doc}, {}, streaming_profile(), index=index, network=network)
    first = search.search(SimpleNamespace(query="bacteria", within=None, offset=0, result_version=None))
    assert first["hits"] and first["result_version"]
    emitted = network.embedded
    reopened = StreamingSearch(
        {"S1": doc},
        {},
        streaming_profile(),
        index=StreamingSearchIndex(journal, ref, streaming_profile(), run_id=run),
        network=network,
    )
    assert network.embedded == emitted
    assert (
        reopened.search(
            SimpleNamespace(query="bacteria", within=None, offset=0, result_version=first["result_version"])
        )
        == first
    )
    with repo.database.transaction() as tx:
        row = one(
            tx,
            "SELECT i.chunk_count,(SELECT count(*) FROM source_search_chunks c WHERE c.project_id=i.project_id AND c.shared_input_record_id=i.shared_input_record_id) AS actual FROM source_search_indexes i WHERE i.shared_input_record_id=:r",
            r=index.params["r"],
        )
        assert row["chunk_count"] == row["actual"] > 32


def test_added_source_ids_are_isolated_between_runs_sharing_one_frozen_input(repo, scope):
    journal, ref = admitted(repo, scope[0])
    a = StreamingSearchIndex(journal, ref, streaming_profile(), run_id=uuid4())
    b = StreamingSearchIndex(journal, ref, streaming_profile(), run_id=uuid4())
    network = TinyModels()
    first = StreamingSearch(
        {"S63": SimpleNamespace(title="A", text="Alpha document exact.", digest="a" * 64)},
        {},
        streaming_profile(),
        index=a,
        network=network,
    )
    second = StreamingSearch(
        {"S63": SimpleNamespace(title="B", text="Beta independently admitted.", digest="b" * 64)},
        {},
        streaming_profile(),
        index=b,
        network=network,
    )
    assert a.params["r"] != b.params["r"] and a.params["h"] == b.params["h"]
    for search, word, other in ((first, "Alpha", "Beta"), (second, "Beta", "Alpha")):
        result = search.search(SimpleNamespace(query=word, within=None, offset=0, result_version=None))
        assert word in result["hits"][0]["preview"] and other not in result["hits"][0]["preview"]
