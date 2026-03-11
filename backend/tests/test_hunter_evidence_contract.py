import json

import pytest

from app.pipeline.phase3_research.hunter import (
    _build_keyword_queries,
    _build_intent_seed_queries,
    _build_hunter_extraction_prompt_payload,
    _dump_hunter_question_debug,
    _extract_evidence_items_with_retry,
    _extract_node_evidence_contract,
    _find_chunk_for_quote,
    _intent_retrieval_goal,
    _merge_required_assets,
    _normalize_single_asset_type,
    _resolve_evidence_origin,
)


def test_extract_node_evidence_contract_new_schema_dedupes_overlap() -> None:
    node = {
        "required_evidence": [
            {"type": "case", "min_count": 1},
            {"type": "CASE", "min_count": 2},
            {"type": "timeline", "min_count": 1},
        ],
        "expected_evidence": ["case", "anecdote", "ANECTODE", "contrast"],
    }

    required, expected = _extract_node_evidence_contract(node)
    assert required == [
        {"type": "CASE", "min_count": 2},
        {"type": "TIMELINE", "min_count": 1},
    ]
    assert expected == ["ANECDOTE", "CONTRAST"]


def test_extract_node_evidence_contract_supports_legacy_expected_evidence_type() -> None:
    node = {"expected_evidence_type": ["mechanism", "definition", "MECHANISM"]}
    required, expected = _extract_node_evidence_contract(node)
    assert required == [
        {"type": "MECHANISM", "min_count": 1},
        {"type": "DEFINITION", "min_count": 1},
    ]
    assert expected == []


def test_merge_required_assets_uses_max_min_count_per_type() -> None:
    aggregate: dict[str, dict[str, int]] = {}
    _merge_required_assets(
        aggregate,
        "b1",
        [{"type": "CASE", "min_count": 1}, {"type": "TIMELINE", "min_count": 1}],
    )
    _merge_required_assets(
        aggregate,
        "b1",
        [{"type": "CASE", "min_count": 3}, {"type": "TIMELINE", "min_count": 1}],
    )
    assert aggregate == {"b1": {"CASE": 3, "TIMELINE": 1}}


def test_normalize_single_asset_type_rejects_invalid_values() -> None:
    assert _normalize_single_asset_type("Mechanism") == "mechanism"
    assert _normalize_single_asset_type("unknown") is None
    assert _normalize_single_asset_type(None) is None


def test_resolve_evidence_origin_uses_chunk_id_and_falls_back_to_quote_matching() -> None:
    chunk_texts = [
        {
            "chunk_id": "11111111-1111-1111-1111-111111111111",
            "source_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "content": "The peg was introduced in 1983 to stabilize the currency market.",
        },
        {
            "chunk_id": "22222222-2222-2222-2222-222222222222",
            "source_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "content": "Banks issue notes against Certificates of Indebtedness under HKMA oversight.",
        },
    ]
    chunk_by_id = {item["chunk_id"]: item for item in chunk_texts}

    item_with_chunk_id = {
        "chunk_id": "22222222-2222-2222-2222-222222222222",
        "verbatim_quote": "Banks issue notes against Certificates of Indebtedness under HKMA oversight.",
    }
    resolved = _resolve_evidence_origin(item_with_chunk_id, chunk_texts, chunk_by_id)
    assert resolved == (
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "22222222-2222-2222-2222-222222222222",
    )

    item_without_chunk_id = {
        "verbatim_quote": "introduced in 1983 to stabilize the currency market",
    }
    resolved = _resolve_evidence_origin(item_without_chunk_id, chunk_texts, chunk_by_id)
    assert resolved == (
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "11111111-1111-1111-1111-111111111111",
    )


def test_find_chunk_for_quote_returns_none_when_not_found() -> None:
    chunk_texts = [{"content": "No matching quote here."}]
    assert _find_chunk_for_quote("This quote does not exist", chunk_texts) is None


def test_find_chunk_for_quote_handles_escaped_currency_symbol() -> None:
    chunk_texts = [
        {
            "chunk_id": "33333333-3333-3333-3333-333333333333",
            "source_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "content": "At one point in September 1983 the exchange rate dropped to a record low of HK\\$9.60 to the US dollar.",
        }
    ]
    quote = "At one point in September 1983 the exchange rate dropped to a record low of HK$9.60 to the US dollar."
    matched = _find_chunk_for_quote(quote, chunk_texts)
    assert matched is not None
    assert matched["chunk_id"] == "33333333-3333-3333-3333-333333333333"


def test_dump_hunter_question_debug_writes_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase3_research.hunter.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_hunter_question_debug(
        run_id="run-1",
        question={
            "id": "b1.1",
            "branch_id": "b1",
            "intent": "explain",
            "priority": 1,
            "question": "Как работает механизм эмиссии?",
            "required_targets": [{"type": "MECHANISM", "min_count": 1}],
            "expected_types": ["CASE"],
        },
        status="ok",
        vector_results=[{"chunk_id": "c1", "source_id": "s1", "score": 0.7}],
        keyword_results=[{"chunk_id": "c2", "source_id": "s2", "score": 0.5}],
        merged_results=[{"chunk_id": "c1", "source_id": "s1", "rrf_score": 0.01}],
        selected_chunks=[{"chunk_id": "c1", "source_id": "s1", "rrf_score": 0.01}],
        loaded_chunks=[{"chunk_id": "c1", "source_id": "s1", "source_title": "Source", "content": "Test content"}],
        extracted_items=[{"asset_type": "MECHANISM", "direct_or_contextual": "direct", "verbatim_quote": "Quote", "locator": {"type": "section", "value": "H"}}],
        saved_items=[{"evidence_id": "e1", "asset_type": "mechanism", "direct_or_contextual": "direct", "source_id": "s1", "chunk_id": "c1", "quote_preview": "Quote"}],
    )

    files = list((tmp_path / "hunter").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-1"
    assert payload["status"] == "ok"
    assert payload["question"]["id"] == "b1.1"
    assert payload["question"]["intent"] == "explain"
    assert payload["retrieval"]["vector_hit_count"] == 1
    assert payload["extraction"]["saved_evidence_count"] == 1
    assert payload["extraction"]["found_expected_evidence"] == {}
    assert payload["extraction"]["retry"]["attempts"] == []


def test_dump_hunter_question_debug_skips_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase3_research.hunter.settings.web_extractor_debug_dump_dir",
        "",
        raising=False,
    )

    _dump_hunter_question_debug(
        run_id="run-1",
        question={"id": "b1", "question": "Q", "required_targets": [], "expected_types": []},
        status="ok",
        vector_results=[],
        keyword_results=[],
        merged_results=[],
        selected_chunks=[],
        loaded_chunks=[],
        extracted_items=[],
        saved_items=[],
    )

    assert list(tmp_path.glob("**/*.json")) == []


@pytest.mark.asyncio
async def test_build_keyword_queries_uses_llm_variants(monkeypatch) -> None:
    captured_prompt: dict = {}

    async def _fake_call(**kwargs) -> str:
        nonlocal captured_prompt
        captured_prompt = json.loads(kwargs["prompt"])
        return '{"queries": ["Hong Kong dollar peg mechanism", "HKD issuance certificates indebtedness"]}'

    monkeypatch.setattr("app.pipeline.phase3_research.hunter.call_haiku_structured", _fake_call)

    queries = await _build_keyword_queries(
        "Как работает эмиссия HKD?",
        "explain",
        [{"type": "MECHANISM", "min_count": 1}],
        ["CASE"],
        [{"preview": "Banks issue notes against Certificates of Indebtedness.", "heading_path": ["Mechanism"]}],
        intent_seed_queries=["How does HKD issuance work mechanism backing"],
    )

    assert captured_prompt["question_intent"] == "explain"
    assert captured_prompt["intent_retrieval_goal"] == "surface direct mechanism and definitions rather than adjacent commentary"
    assert captured_prompt["intent_seed_queries"] == ["How does HKD issuance work mechanism backing"]
    assert captured_prompt["required_evidence"] == [{"type": "MECHANISM", "min_count": 1}]
    assert captured_prompt["expected_evidence"] == ["CASE"]
    assert queries[0] == "Как работает эмиссия HKD?"
    assert "Hong Kong dollar peg mechanism" in queries


@pytest.mark.asyncio
async def test_build_keyword_queries_keeps_intent_seed_queries_when_vector_results_empty() -> None:
    queries = await _build_keyword_queries(
        "Why is private note issuance more reliable?",
        "debate",
        [{"type": "MECHANISM", "min_count": 1}],
        ["CONTRAST"],
        [],
        intent_seed_queries=[
            "Why is private note issuance more reliable arguments tradeoffs criticism reliability versus monopoly",
            "Why is private note issuance more reliable supporters opponents comparison advantages disadvantages",
        ],
    )

    assert queries[0] == "Why is private note issuance more reliable?"
    assert len(queries) == 3
    assert any("tradeoffs" in query.lower() for query in queries[1:])


def test_build_intent_seed_queries_for_debate_include_argument_language() -> None:
    queries = _build_intent_seed_queries(
        "Why is private note issuance more reliable?",
        "debate",
    )

    assert len(queries) == 2
    assert any("arguments" in query.lower() for query in queries)
    assert any("tradeoffs" in query.lower() or "opponents" in query.lower() for query in queries)


def test_intent_retrieval_goal_for_case_study_mentions_concrete_episodes() -> None:
    goal = _intent_retrieval_goal("case_study")
    assert "concrete episodes" in goal
    assert "consequences" in goal


@pytest.mark.asyncio
async def test_extract_evidence_items_with_retry_retries_after_exception_with_narrower_payload(monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_call(**kwargs) -> str:
        prompt = json.loads(kwargs["prompt"])
        calls.append(
            {
                "system": kwargs["system"],
                "chunk_count": len(prompt["chunks"]),
                "has_retry_focus": "retry_focus" in prompt,
            }
        )
        if len(calls) == 1:
            raise RuntimeError("LLM timeout")
        return '[{"chunk_index": 0, "verbatim_quote": "Quote", "asset_type": "TIMELINE"}]'

    monkeypatch.setattr("app.pipeline.phase3_research.hunter.call_haiku_structured", _fake_call)

    items, status, meta = await _extract_evidence_items_with_retry(
        question={
            "question": "How did the exchange-rate regime change?",
            "intent": "history",
            "required_targets": [{"type": "TIMELINE", "min_count": 1}],
            "expected_types": ["CASE"],
        },
        chunk_texts=[
            {"content": f"Chunk {i}", "source_title": "Source", "heading_path": []}
            for i in range(8)
        ],
    )

    assert status == "ok"
    assert isinstance(items, list) and len(items) == 1
    assert calls[0]["chunk_count"] == 8
    assert calls[1]["chunk_count"] == 4
    assert calls[1]["has_retry_focus"] is True
    assert meta["attempt_count"] == 2
    assert meta["attempts"][0]["status"] == "exception"
    assert meta["attempts"][1]["status"] == "ok"


@pytest.mark.asyncio
async def test_extract_evidence_items_with_retry_uses_strict_format_after_invalid_payload(monkeypatch) -> None:
    systems: list[str] = []

    async def _fake_call(**kwargs) -> str:
        systems.append(kwargs["system"])
        if len(systems) == 1:
            return '{"not":"a-list"}'
        return '[{"chunk_index": 0, "verbatim_quote": "Quote", "asset_type": "CASE"}]'

    monkeypatch.setattr("app.pipeline.phase3_research.hunter.call_haiku_structured", _fake_call)

    items, status, meta = await _extract_evidence_items_with_retry(
        question={
            "question": "Which episode illustrates the policy shift?",
            "intent": "case_study",
            "required_targets": [{"type": "CASE", "min_count": 1}],
            "expected_types": ["ANECDOTE"],
        },
        chunk_texts=[
            {"content": "Chunk 1", "source_title": "Source", "heading_path": []},
            {"content": "Chunk 2", "source_title": "Source", "heading_path": []},
        ],
    )

    assert status == "ok"
    assert isinstance(items, list) and len(items) == 1
    assert "STRICT FORMAT" not in systems[0]
    assert "STRICT FORMAT" in systems[1]
    assert meta["attempts"][0]["status"] == "invalid_payload"
    assert meta["attempts"][1]["status"] == "ok"


def test_build_hunter_extraction_prompt_payload_adds_retry_focus_only_for_retry() -> None:
    payload = _build_hunter_extraction_prompt_payload(
        question={
            "question": "How does the mechanism work?",
            "intent": "explain",
            "required_targets": [{"type": "MECHANISM", "min_count": 1}],
            "expected_types": ["CASE"],
        },
        chunk_texts=[{"content": "Chunk", "source_title": "Source", "heading_path": ["H1"]}],
        directness_push=False,
    )
    retry_payload = _build_hunter_extraction_prompt_payload(
        question={
            "question": "How does the mechanism work?",
            "intent": "explain",
            "required_targets": [{"type": "MECHANISM", "min_count": 1}],
            "expected_types": ["CASE"],
        },
        chunk_texts=[{"content": "Chunk", "source_title": "Source", "heading_path": ["H1"]}],
        directness_push=True,
    )

    assert "retry_focus" not in payload
    assert "retry_focus" in retry_payload
