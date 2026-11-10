"""Discovery/read separation, compact exact previews, bounds and failure-safe model installation."""

import hashlib
import io
import json

import pytest
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.assets import prepare, verify
from podgen.retrieval.config import profile
from podgen.retrieval.hybrid import HybridSearch
from podgen.retrieval.memory import MemoryIndex
from podgen.writing.source_tools.contracts import Search
from podgen.writing.source_tools.corpus import Source

from tests.source_search_support import TinyModels


def document(text, title="Original title"):
    return Source(title, text, hashlib.sha256(text.encode()).hexdigest())


def test_exact_compact_preview_full_read_ranges_scope_and_stable_pagination():
    raw = ("Начало фрагмента. " * 8 + "Identifier QX-1854 is important. ё🪨\n") * 120
    docs = {"S1": document(raw, "Документ ё🪨" * 200), "S2": document("Private unrelated source.")}
    engine = HybridSearch(docs, {"M1": [("S1", 0, 10)]}, profile(), index=MemoryIndex(), network=TinyModels())
    cursor = 0
    coordinates = []
    while cursor is not None:
        result = engine.search(Search(query="QX-1854", within="M1", offset=cursor))
        assert (
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            <= profile()["max_response_bytes"]
        )
        assert "scores" not in json.dumps(result) and "read_id" not in json.dumps(result)
        for hit in result["hits"]:
            assert (
                hit["source_id"] == "S1" and raw[hit["preview_start"] : hit["preview_end"]] == hit["preview"]
            )
            assert hit["start"] <= hit["preview_start"] < hit["preview_end"] <= hit["end"]
            coordinates.append((hit["start"], hit["end"]))
        cursor = result["next_offset"]
    assert len(coordinates) == len(set(coordinates))
    assert engine.search(Search(query="x", within="S2", offset=0))["sources"].keys() == {"S2"}
    assert engine.diagnostics(Search(query="QX-1854", within="M1", offset=0))["ranked"][0]["scores"]


def test_literal_match_moves_preview_and_original_text_is_unchanged():
    raw = "unchanged context " * 50 + "QX-1854 exact identifier. tail"
    from podgen.retrieval.preview import page

    doc = document(raw)
    result = page(
        [dict(source_id="S1", start=0, end=len(raw), read_start=0)], {"S1": doc}, "QX-1854", 0, profile()
    )
    hit = result["hits"][0]
    assert hit["preview_start"] > 0 and "QX-1854" in hit["preview"]
    assert raw[hit["preview_start"] : hit["preview_end"]] == hit["preview"] and doc.text == raw


def test_corpus_limit_refuses_before_inference_and_no_index_is_silently_substituted():
    network = TinyModels()
    with pytest.raises(RetrievalUnavailable, match="corpus_limit"):
        HybridSearch(
            {f"S{i}": document("x") for i in range(65)}, {}, profile(), index=MemoryIndex(), network=network
        )
    assert network.embedded == 0
    with pytest.raises(RetrievalUnavailable, match="corpus_limit"):
        HybridSearch({"S1": document("x" * 2000001)}, {}, profile(), index=MemoryIndex(), network=network)
    assert network.embedded == 0


def test_interrupted_model_download_never_publishes_partial_model_and_retry_recovers(tmp_path, monkeypatch):
    from podgen.retrieval import assets

    monkeypatch.setenv("PODGEN_SEARCH_CACHE", str(tmp_path))
    body = b"fixture model bytes"
    spec = dict(
        repository="fixture/model",
        revision="immutable",
        files={"model.bin": hashlib.sha256(body).hexdigest()},
    )
    monkeypatch.setattr(assets, "ENCODER", spec)
    monkeypatch.setattr(assets, "RERANKER", spec)

    class Broken(io.BytesIO):
        def read(self, *args):
            raise OSError("interrupted")

    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *args, **kwargs: Broken(body))
    with pytest.raises(OSError):
        prepare()
    assert not list(tmp_path.rglob("*.part"))
    with pytest.raises(RetrievalUnavailable):
        verify(spec)
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(body))
    prepare()
    assert (verify(spec) / "model.bin").read_bytes() == body


def test_multibyte_preview_budget_reduces_page_without_losing_results():
    from podgen.retrieval.preview import page

    docs = {f"S{i}": document("🪨" * 400, "🌍" * 200) for i in range(8)}
    rows = [dict(source_id=key, start=0, end=400, read_start=0) for key in docs]
    cursor, seen = 0, []
    while cursor is not None:
        result = page(rows, docs, "", cursor, profile())
        assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 6144
        assert 0 < len(result["hits"]) < 6
        seen.extend(hit["source_id"] for hit in result["hits"])
        cursor = result["next_offset"]
    assert seen == list(docs)
