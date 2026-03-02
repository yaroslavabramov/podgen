from app.pipeline.phase3_research.hunter import (
    _extract_node_evidence_contract,
    _find_chunk_for_quote,
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
