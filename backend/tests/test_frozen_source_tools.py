from hashlib import sha256
from types import SimpleNamespace as NS

import pytest

from podgen.domain.errors import ContractError
from podgen.writing.revision.assignment import tasks
from podgen.writing.source_tools import SourceCorpus, attach_task_passages, definitions, resolve_review_reads


def materials(text="Old card.\nAn ob-\nservation was made after the card ended.\n"):
    body = text
    digest = sha256(body.encode()).hexdigest()
    src = NS(key="k", title="Book", purpose="primary", source_path="raw/book", sha256=digest)
    card = NS(excerpts=[NS(document="k", start=0, end=9)])
    dossier = NS(knowledge_cards=[card])
    return NS(
        packet=NS(documents=[src], dossier=dossier),
        documents=[NS(path=src.source_path, text=body, sha256=digest)],
    )


def read(corpus, start=0, end=None, alias="E1"):
    return corpus.execute("read_source", dict(source_id="S1", start=start, end=end), read_id=alias)


def search(corpus, query, within=None, offset=0):
    return corpus.execute("search_sources", dict(query=query, within=within, offset=offset), read_id="unused")


def finding(ids):
    return dict(
        priority="important",
        anchors=[],
        support_labels=[],
        observation="Attribution is missing.",
        requested_change="Attribute the estimate.",
        preserve=[],
        source_read_ids=ids,
    )


def test_search_is_discovery_and_exact_read_can_extend_beyond_a_card():
    corpus = SourceCorpus(materials())
    assert search(corpus, "")["sources"][0]["source_id"] == "S1"
    assert search(corpus, "", "M1")["hits"][0]["end"] == 9
    hit = search(corpus, "observation", "M1")["hits"][0]
    assert "read_id" not in hit and hit["start"] == 13
    passage = read(corpus, hit["start"], hit["end"])["passage"]
    assert passage["text"] == "ob-\nservation" and passage["read_id"] == "E1"
    assert {t["name"] for t in definitions()} == {"search_sources", "read_source"}
    assert search(corpus, "zzz")["hits"] == []


def test_read_pages_and_bad_arguments_do_not_change_the_source():
    source = "ёx" * 4000
    corpus = SourceCorpus(materials(source))
    rows = []
    cursor = 0
    while cursor is not None:
        result = read(corpus, cursor)
        rows.append(result["passage"]["text"])
        cursor = result["next_start"]
    assert "".join(rows) == source
    assert read(corpus, -1)["error"] == "invalid_source_tool_arguments"
    assert read(corpus, 1, len(source) + 2)["error"] == "invalid_source_range"
    broken = materials(source)
    broken.documents[0].text = "edited"
    with pytest.raises(ContractError, match="source_tool_frozen_document_mismatch"):
        SourceCorpus(broken)


def test_only_delivered_reads_reach_the_matching_writer_tasks_and_are_shared_once():
    m = materials()
    opened = read(SourceCorpus(m), 9)["passage"]
    original = dict(findings=[finding(["E1"]), finding(["E1"]), finding([])])
    fact = resolve_review_reads(original, [opened], m)
    assert original["findings"][0]["source_read_ids"] == ["E1"]
    payload = {"revision_tasks": tasks({"reader": {"findings": []}, "fact": fact}, [])}
    attach_task_passages(payload, m)
    assert (
        payload["revision_tasks"][0]["source_read_refs"]
        == payload["revision_tasks"][1]["source_read_refs"]
        == ["P1"]
    )
    assert "source_read_refs" not in payload["revision_tasks"][2]
    assert len(payload["review_source_passages"]) == 1
    passage = payload["review_source_passages"][0]
    assert passage["text"] == opened["text"] and passage["ref"] == "P1"
    assert m.documents[0].sha256 not in str(payload)
    with pytest.raises(ContractError, match="unread_or_duplicate"):
        resolve_review_reads(original, [], m)
    with pytest.raises(ContractError, match="source_read_receipt_mismatch"):
        resolve_review_reads(original, [dict(opened, text="hallucinated")], m)
    with pytest.raises(ContractError, match="invalid_source_read_locator"):
        SourceCorpus(materials("Other revision")).locate(fact["findings"][0]["source_read_ids"][0])


def test_explicit_range_uses_its_pinned_budget_and_open_ended_reads_stay_small():
    corpus = SourceCorpus(materials("а" * 15000))
    args = {"source_id": "S1", "start": 100, "end": 6700}
    result = corpus.execute("read_source", args, read_id="E3", max_read_chars=9600)
    assert result["next_start"] is None
    assert result["passage"]["text"] == "а" * 6600
    assert read(corpus, 100, 6700)["next_start"] == 2500
    assert (
        corpus.execute("read_source", dict(args, end=None), read_id="E4", max_read_chars=9600)["next_start"]
        == 2500
    )
    assert (
        corpus.execute("read_source", dict(args, end=15000), read_id="E5", max_read_chars=9600)["next_start"]
        == 9700
    )
    stored = resolve_review_reads(
        {"findings": [finding(["E3"])]}, [result["passage"]], materials("а" * 15000)
    )
    assert corpus.locate(stored["findings"][0]["source_read_ids"][0])["end"] == 6700
