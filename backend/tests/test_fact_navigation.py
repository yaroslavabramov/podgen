"""Fact-only navigation through product tools, metadata restoration and frozen views."""

import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from podgen.runtime.models.fact_navigation import entry_catalog
from podgen.runtime.models.research_history import ResearchHistory
from podgen.runtime.models.research_tools import ResearchTools
from podgen.writing.research_materials.fact_navigation import KEY, VERSION, enabled
from tests.test_research_runtime import research_setup


def setup(monkeypatch, *, enabled=True):
    journal, _, _, corpus = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    corpus.documents = {"S1": NS(digest=sha256(b"retained source").hexdigest())}
    corpus.result = dict(
        hits=[dict(source_id="S1", start=0, end=100, preview_start=10, preview_end=14, preview="text")]
    )
    corpus.execute = lambda *args, **kwargs: deepcopy(corpus.result)
    history = ResearchHistory(journal, journal.root)
    proxy = ResearchTools(
        journal,
        journal.root,
        corpus,
        history,
        lambda: None,
        history_view="research-work-history/1",
        fact_navigation=enabled,
    )
    return journal, corpus, history, proxy


def delivered(journal, history, proxy, result, index=0):
    data = dict(
        name="search_sources",
        arguments=dict(query="query"),
        result=result,
        call_index=index,
        call_id=f"search{index}",
        turn_index=index,
        search_delivery_identity=proxy.last_search_delivery,
    )
    ref = journal.event(journal.root, "source_tool_result", data=data)
    history.appended(ref, "source_tool_result", data=data)
    return data


def search(proxy, index=0):
    return proxy.execute(
        "search_sources", dict(query="query", within=None, offset=0), read_id=f"E{index + 1}"
    )


def test_exact_preview_and_different_quote_remain_distinct_after_checkpoint_and_retry(monkeypatch):
    journal, corpus, history, proxy = setup(monkeypatch)
    first = search(proxy)
    assert first == corpus.result  # No bookkeeping in an ordinary first result.
    data = delivered(journal, history, proxy, first)
    journal.event(journal.root, "source_context_segment", data=dict(next_turn=2, checkpoint_call_index=1))
    retry = dict(
        journal.root,
        invocation_id=uuid4(),
        attempt_kind="credit_retry",
        parent_invocation_id=journal.root["invocation_id"],
    )
    journal.roots[retry["invocation_id"]] = retry
    restored = ResearchHistory(journal, retry)
    proxy.history = restored
    assert search(proxy)["hits"][0]["previous_result"] == dict(index=0, match="same_text")
    corpus.result["hits"][0].update(preview="else", preview_start=14, preview_end=18)
    assert search(proxy)["hits"][0]["previous_result"] == dict(index=0, match="same_location_new_excerpt")
    # Technical identities never enter work-history results.
    page = proxy.execute(
        "read_work_history", dict(kind="tool_result", index=0, start=0, end=None), read_id="E3"
    )
    assert json.loads(page["text"])["result"] == first
    assert "search_delivery_identity" not in page["text"]
    assert data["search_delivery_identity"]["passages"]


@pytest.mark.parametrize("change", ["version", "range", "assessment", "missing_locator"])
def test_no_document_wide_or_cross_assessment_seen_claim(monkeypatch, change):
    journal, corpus, history, proxy = setup(monkeypatch)
    delivered(journal, history, proxy, search(proxy))
    if change == "version":
        corpus.documents["S1"].digest = sha256(b"new source").hexdigest()
    elif change == "range":
        corpus.result["hits"][0].update(start=100, end=200, preview_start=110, preview_end=114)
    elif change == "missing_locator":
        corpus.result["hits"][0].pop("preview_start")
    else:
        root = dict(journal.root, invocation_id=uuid4())
        proxy.history = ResearchHistory(journal, root)
    assert "previous_result" not in search(proxy)["hits"][0]


def test_legacy_search_and_history_errors_are_unchanged(monkeypatch):
    journal, corpus, history, proxy = setup(monkeypatch, enabled=False)
    delivered(journal, history, proxy, search(proxy))
    assert search(proxy) == corpus.result
    error = proxy.execute(
        "read_work_history", dict(kind="tool_result", index=0, start=0, end=50000), read_id="E3"
    )
    assert set(error) == {"kind", "index", "error", "length"}
    proxy.fact_navigation = True
    error = proxy.execute(
        "read_work_history", dict(kind="tool_result", index=0, start=0, end=50000), read_id="E3"
    )
    assert error["max_page_characters"] == 9600
    assert error["retry"] == dict(start=0, end=error["length"])


def test_entry_catalog_is_paged_metadata_and_fact_only():
    class Unread:
        def __str__(self):
            pytest.fail("report text entered the catalog")

    materials = NS(
        runtime_admissions=[
            NS(
                source_id=f"X{i}",
                title=f"Exact topic {i}",
                sha256=str(i),
                text=Unread(),
                catalog=dict(kind="research_report"),
            )
            for i in range(1, 8)
        ]
    )
    page = entry_catalog(materials)
    assert len(page["reports"]) == 6 and page["next_page"]["offset"] == 6
    assert page["reports"][0]["question"] == "Exact topic 1"
    materials.runtime_admissions = materials.runtime_admissions[:1]
    assert entry_catalog(materials) == dict(reports=[dict(result_id="X1", question="Exact topic 1")])
    assert enabled({KEY: VERSION}, "fact-review-1")
    for stage in ("plan", "draft", "revise-1", "reader-review", "reader-review-1"):
        assert not enabled({KEY: VERSION}, stage)


def test_unavailable_model_view_and_history_keep_technical_receipt_separate(monkeypatch):
    journal, corpus, history, proxy = setup(monkeypatch)
    full = dict(
        status="unavailable",
        url="https://example.org/document",
        diagnostics=[],
        attempt_ref=dict(record_kind="tool_execution", record_id=str(uuid4())),
        representations=[
            dict(
                source_revision_ref="technical",
                diagnostics=["cloudfront_error_page", "access_denied", "http_status_403"],
            )
        ],
    )
    expected = dict(status="unavailable", url=full["url"], reason="http_status_403")
    assert proxy.model_result("request_source", full) == expected
    data = dict(
        name="request_source",
        arguments=dict(url=full["url"]),
        result=full,
        call_index=0,
        call_id="missing",
        turn_index=0,
    )
    ref = journal.event(journal.root, "source_tool_result", data=data)
    history.appended(ref, "source_tool_result", data=data)
    page = proxy.execute(
        "read_work_history", dict(kind="tool_result", index=0, start=0, end=None), read_id="E1"
    )
    assert json.loads(page["text"])["result"] == expected
    assert journal.read(ref)["data"]["result"] == full
    proxy.fact_navigation = False
    assert proxy.model_result("request_source", full) == full


def test_native_wire_keeps_repeat_pointer_but_no_identity_journal(monkeypatch):
    import httpx2

    from podgen.runtime.models.gateway import ModelGateway
    from podgen.runtime.models.openai_provider import OpenAIProvider
    from tests.test_research_runtime import answer, final, invoke

    journal, profile, limits, corpus = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    corpus.documents = {"S1": NS(digest=sha256(b"source").hexdigest())}
    result = dict(
        hits=[dict(source_id="S1", start=0, end=100, preview_start=0, preview_end=4, preview="text")]
    )
    corpus.execute = lambda *a, **kw: deepcopy(result)
    sent = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        serialized = json.dumps(body["input"])
        assert "search_delivery_identity" not in serialized
        assert "passage_id" not in serialized and "preview_id" not in serialized
        if turn in (0, 2):
            output = dict(
                type="function_call",
                call_id=f"search{turn}",
                name="search_sources",
                arguments=json.dumps(
                    dict(query="text", within=None, offset=0, result_version=None, aliases_of=None)
                ),
            )
        elif turn == 1:
            output = dict(
                type="function_call",
                call_id="save",
                name="save_working_state",
                arguments=json.dumps(
                    dict(state="Full state preserved.", resume_state="Check the next claim.", receipt_ids=[])
                ),
            )
        elif turn == 3:
            assert "same_text" in serialized and "previous_result" in serialized
            output = dict(
                type="function_call",
                call_id="history",
                name="read_work_history",
                arguments=json.dumps(dict(kind="tool_result", index=0, start=0, end=None)),
            )
        else:
            output = final()
        return answer(body, turn, [output])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("offline-unique-key", http_client=client), source_corpus=corpus
        )
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        before = len(sent)
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert len(sent) == before
    records = [
        journal.read(NS(record_id=e["record_id"]))
        for e in journal.events(journal.root)
        if e["event_kind"] == "source_tool_result"
    ]
    searches = [r for r in records if r["data"]["name"] == "search_sources"]
    assert searches[0]["data"]["result"] == result
    assert searches[1]["data"]["result"]["hits"][0]["previous_result"] == dict(index=0, match="same_text")
    assert searches[0]["data"]["search_delivery_identity"]["passages"]


def test_saved_preview_keeps_initial_catalog_after_later_admissions(monkeypatch):
    from podgen.research.materials.resolver import ResearchMaterialResolver
    from podgen.runtime.evaluation.compiler import materials_hash
    from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot

    journal, _, _, _ = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    manifest = journal.snapshot
    manifest["source_tool_contract"][KEY] = VERSION
    manifest["materials_hash"] = materials_hash({})
    manifest["prompt_refs"] = {}
    frozen = dict(reports=[dict(result_id="X1", question="Original question, exactly.")])
    journal.root["request_record_id"] = journal.store_request(
        dict(
            input=[
                dict(
                    role="user",
                    content=[dict(type="input_text", text=json.dumps(dict(research_results=frozen)))],
                )
            ]
        )
    )
    journal.run = lambda _: True
    journal.invocation = lambda *_: journal.root
    materials = NS(as_payload=lambda: {}, runtime_admissions=[], shared_ref=None)
    journal.repo.get_runtime = lambda *a: dict(preparation={})
    later = NS(source_id="X2", title="Later question")
    monkeypatch.setattr(ResearchMaterialResolver, "restore", lambda *a, **kw: [later])
    monkeypatch.setattr("podgen.runtime.evaluation.research_annex.restore", lambda *a: None)
    snapshot = load_preview_snapshot(journal, None, journal.root["run_id"], "fact-review", materials)
    assert snapshot.fact_navigation_view == frozen
    assert materials.runtime_admissions == [later]
    manifest["source_tool_contract"].pop(KEY)
    legacy = load_preview_snapshot(journal, None, journal.root["run_id"], "fact-review", materials)
    assert legacy.fact_navigation_view is None
