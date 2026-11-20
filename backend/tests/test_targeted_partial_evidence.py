"""A saved paid answer is reused while only literal source matches become evidence."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from podgen.research.hosted.journal import ResearchJournal
from podgen.research.materials.resolver import ResearchMaterialResolver
from podgen.research.materials.targeted_reparse import VERSION, cached_result
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.source_acquisition_support import ARTICLE, document
from tests.test_research_material_resolver import fixture, save_report


def test_completed_legacy_result_remains_exact():
    old = dict(status="resolved", contract="targeted-research-answer/1", answer="Accepted saved answer.")
    store = SimpleNamespace(get=lambda key: old if key == "request:result" else None)
    key, saved = cached_result(
        store, "request", "request_research", dict(research_contract="targeted-script-context/1")
    )
    assert key == "request:result" and saved is old


@pytest.mark.parametrize("failure", ["valid", "fabricated", "unavailable", "invalid_url", "duplicate_key"])
@pytest.mark.parametrize("cached", [False, True])
def test_partial_evidence_is_retained_without_admitting_answer_or_repeating_research(
    repo, scope, producer, monkeypatch, failure, cached
):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(
        question="What are the exact words?",
        reason="Verify the claim.",
        research_contract="targeted-script-context/1",
        anchors=[],
        script_context=None,
    )
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    url = "https://example.com/article"
    bad_url = (
        "https://example.com/missing"
        if failure == "unavailable"
        else "file:///document"
        if failure == "invalid_url"
        else url
    )
    if failure == "unavailable":
        fetcher.documents[bad_url] = document("Not found", url=bad_url, status=404)
    text = json.dumps(
        dict(
            answer="A provider claim requiring verification.",
            limitations="",
            evidence=[
                dict(url=url, quote=ARTICLE[:80], relation="Exact words."),
                dict(url=bad_url, quote="A fabricated quote.", relation="Unverified claim."),
            ],
        )
    )
    if failure == "valid":
        value = json.loads(text)
        value["evidence"].pop()
        text = json.dumps(value)
    if failure == "duplicate_key":
        text = text.replace('"answer":', '"answer":"competing answer", "answer":')
    sources = (Path(__file__).parent / "fixtures/gemini_deep_research_sources.md").read_text()
    save_report(child, f"```json\n{text}\n```\n\n{sources}")
    old = dict(status="unresolved", error="targeted_research_invalid_answer", question=args["question"])
    store, key = resolver.store(run_id), "material-request:" + str(operation)
    if cached:
        store.put(key + ":result", old)
        monkeypatch.setattr(
            "podgen.research.materials.research_requests.prepare",
            lambda *a, **kw: pytest.fail("saved request reconstructed"),
        )
    frozen = child.get("manifest"), child.get("result")
    monkeypatch.setattr(
        "podgen.research.hosted.service.advance", lambda *a, **kw: pytest.fail("paid research repeated")
    )
    if cached and failure == "valid":
        put = ResearchJournal.put

        def interrupted(journal, record_key, value):
            result = put(journal, record_key, value)
            if journal.run_id == run_id and record_key == key + ":validation:" + VERSION + ":result":
                raise RuntimeError("Interrupted after derived result publication")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(ResearchJournal, "put", interrupted)
            with pytest.raises(RuntimeError, match="derived result publication"):
                resolver.resolve(
                    "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
                )
        before_replay = len(fetcher.calls)
    result = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    if failure == "valid":
        assert result.result["status"] == "resolved"
        assert result.result["report_id"] == "X1"
        assert result.result["excerpts"][0]["quote"] == ARTICLE[:80]
    else:
        assert result.result["status"] == "unresolved"
        assert "answer" not in result.result and "report_id" not in result.result
        assert not any(a.source_id.startswith("X") for a in result.admissions)
    if failure == "duplicate_key":
        assert result.result["error"] == "targeted_research_invalid_answer"
        assert "excerpts" not in result.result
    elif failure != "valid":
        assert result.result["contract"] == "targeted-research-partial-evidence/1"
        assert result.result["excerpts"] == [
            dict(
                source_id="S11",
                quote=ARTICLE[:80],
                start=0,
                end=80,
                relation="Exact words.",
            )
        ]
        assert result.result["rejected_evidence"][0]["evidence_index"] == 1
        assert result.result["unverified_answer"] == "A provider claim requiring verification."
    before = len(fetcher.calls)
    again = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire).resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert again == result
    assert len(fetcher.calls) == before
    assert (child.get("manifest"), child.get("result")) == frozen
    assert result.result["validation_contract"] == VERSION
    if cached:
        assert store.get(key + ":result") == old
        derived_key = key + ":validation:" + VERSION + ":result"
        derived = store.get(derived_key)
        assert derived["result"] == result.result
        assert derived["original_result_ref"] == store.ref(key + ":result").model_dump(mode="json")
        if failure == "valid":
            assert len(fetcher.calls) == before_replay
