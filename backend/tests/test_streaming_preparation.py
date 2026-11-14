"""Bounded reads and unchanged coordinates without a database or inference runtime."""

from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace

import pytest

from podgen.persistence.text_source import VerifiedText
from podgen.retrieval.config import streaming_profile
from podgen.retrieval.streaming import StreamingSearch
from podgen.retrieval.streaming_chunks import batches, prepared_batches
from tests.source_search_support import TinyModels


def test_prepared_passages_reuse_exact_unicode_window_and_coordinates():
    class Text:
        def __init__(self):
            self.value = "Original ёжик 😀\r\n" * 1500
            self.reads = []

        def __len__(self):
            return len(self.value)

        def __getitem__(self, key):
            self.reads.append((key.start, key.stop))
            return self.value[key]

    text, profile, network = Text(), streaming_profile(), TinyModels()
    prepared = list(prepared_batches(text, network, profile))
    assert [(a, b) for a, b, _ in prepared] == list(batches(text.value, network, profile))
    assert all(
        fragment == text.value[a:b]
        for ranges, _, fragments in prepared
        for (a, b), fragment in zip(ranges, fragments, strict=True)
    )
    assert len(text.reads) < sum(len(ranges) for ranges, _, _ in prepared)
    assert max(end - start for start, end in text.reads) <= profile["tokenization_characters"]


def test_completed_preparation_never_loads_inference_models(monkeypatch):
    documents = {
        f"S{i}": SimpleNamespace(
            title="Original",
            text="A short retained original.",
            digest=sha256(b"A short retained original.").hexdigest(),
        )
        for i in range(130)
    }
    groups = []
    access_checks = []

    class Ready:
        def refresh_dependencies(self):
            access_checks.append(True)

        def prepare_documents(self, group):
            groups.append(group)
            return {key for key, _, _ in group}

        def document(self, *args):
            pytest.fail("completed computation reopened for building")

    monkeypatch.setattr("podgen.retrieval.streaming.models", lambda _: pytest.fail("inference models loaded"))
    search = StreamingSearch(documents, {}, streaming_profile(), index=Ready())
    search.refresh()
    assert [len(group) for group in groups] == [64, 64, 2]
    assert access_checks == [True]


def test_source_snapshots_end_before_embedding_and_passages_are_not_read_again(monkeypatch):
    state = dict(active=False, reads=0)

    @contextmanager
    def operation(repo):
        assert not state["active"]
        state["active"] = True
        try:
            yield
        finally:
            state["active"] = False

    class Text(VerifiedText):
        def __init__(self):
            self.repo = object()
            self.prepared = None
            self.value = "Original bacteria. " * 1500

        def __len__(self):
            return len(self.value)

        def __getitem__(self, key):
            assert state["active"]
            state["reads"] += 1
            return self.value[key]

    class Network(TinyModels):
        def embed(self, texts):
            assert not state["active"]
            return super().embed(texts)

    class Index:
        @contextmanager
        def document(self, *args):
            assert not state["active"]
            yield dict(completed=False, cursor_position=0)

        def append_batch(self, source, doc, ranges, code, vectors, *, texts, **options):
            assert not state["active"]
            assert texts == [doc.text.value[a:b] for a, b in ranges]

    monkeypatch.setattr("podgen.persistence.source_reading.read_operation", operation)
    text = Text()
    doc = SimpleNamespace(title="Original", text=text, digest=sha256(text.value.encode()).hexdigest())
    StreamingSearch({"S1": doc}, {}, streaming_profile(), index=Index(), network=Network())
    # One language head plus bounded tokenizer windows; no per-chunk source reads.
    assert state["reads"] == 1 + len(list(prepared_batches(text.value, TinyModels(), streaming_profile())))
