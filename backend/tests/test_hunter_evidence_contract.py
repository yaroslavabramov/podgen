from app.pipeline.phase3_research.hunter import (
    _extract_node_evidence_contract,
    _merge_required_assets,
    _merge_target_types,
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
    assert _merge_target_types(required, expected) == ["CASE", "TIMELINE", "ANECDOTE", "CONTRAST"]


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
