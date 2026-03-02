import pytest

from app.pipeline.phase2_decomposition.decomposer import _sanitize_and_validate_research_tree


def test_sanitize_and_validate_research_tree_normalizes_required_expected_and_removes_legacy_hints() -> None:
    payload = {
        "root_question": "  Why this system works?  ",
        "branches": [
            {
                "id": "b1",
                "question": "  How does issuance work? ",
                "intent": "EXPLAIN",
                "required_evidence": [
                    {"type": "mechanism", "min_count": 1},
                    {"type": "MECHANISM", "min_count": 2},
                    {"type": "definition", "min_count": 1},
                ],
                "expected_evidence": ["definition", "contrast", "mechanism"],
                "quality_need": "verified",
                "priority": 1,
                "explanation_hints": ["legacy field"],
                "children": [
                    {
                        "id": "b1.1",
                        "question": " What is currency board? ",
                        "intent": "Explain",
                        "required_evidence": [{"type": "definition", "min_count": 1}],
                        "expected_evidence": ["contrast", "CASE"],
                        "quality_need": "observed",
                        "priority": 2,
                        "explanation_hints": ["legacy child field"],
                    }
                ],
            }
        ],
    }

    result = _sanitize_and_validate_research_tree(payload)

    assert result["root_question"] == "Why this system works?"
    branch = result["branches"][0]
    assert "explanation_hints" not in branch
    assert branch["intent"] == "explain"
    assert branch["required_evidence"] == [
        {"type": "MECHANISM", "min_count": 2},
        {"type": "DEFINITION", "min_count": 1},
    ]
    assert branch["expected_evidence"] == ["CONTRAST"]
    assert branch["quality_need"] == "VERIFIED"

    child = branch["children"][0]
    assert "explanation_hints" not in child
    assert child["intent"] == "explain"
    assert child["required_evidence"] == [{"type": "DEFINITION", "min_count": 1}]
    assert child["expected_evidence"] == ["CONTRAST", "CASE"]
    assert child["quality_need"] == "OBSERVED"


def test_sanitize_and_validate_research_tree_raises_on_non_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        _sanitize_and_validate_research_tree([])


def test_sanitize_and_validate_research_tree_supports_legacy_expected_evidence_type() -> None:
    payload = {
        "root_question": "Q",
        "branches": [
            {
                "id": "b1",
                "question": "Branch question",
                "intent": "history",
                "expected_evidence_type": ["timeline", "CASE", "timeline"],
                "quality_need": "VERIFIED",
                "priority": 1,
                "children": [],
            }
        ],
    }

    result = _sanitize_and_validate_research_tree(payload)
    branch = result["branches"][0]
    assert branch["required_evidence"] == [
        {"type": "TIMELINE", "min_count": 1},
        {"type": "CASE", "min_count": 1},
    ]
    assert branch["expected_evidence"] == []


def test_sanitize_and_validate_research_tree_raises_on_missing_required_fields() -> None:
    payload = {
        "root_question": "Q",
        "branches": [
            {
                "id": "b1",
                "intent": "history",
                "required_evidence": [{"type": "TIMELINE", "min_count": 1}],
                "expected_evidence": [],
                "quality_need": "VERIFIED",
                "priority": 1,
                "children": [],
            }
        ],
    }

    with pytest.raises(ValueError, match="question"):
        _sanitize_and_validate_research_tree(payload)


def test_sanitize_and_validate_research_tree_raises_on_invalid_child_schema() -> None:
    payload = {
        "root_question": "Q",
        "branches": [
            {
                "id": "b1",
                "question": "Branch question",
                "intent": "history",
                "required_evidence": [{"type": "TIMELINE", "min_count": 1}],
                "expected_evidence": [],
                "quality_need": "VERIFIED",
                "priority": 1,
                "children": [
                    {
                        "id": "child-1",
                        "question": "Child question",
                        "intent": "explain",
                        "required_evidence": [{"type": "DEFINITION", "min_count": 1}],
                        "expected_evidence": [],
                        "quality_need": "VERIFIED",
                        "priority": 1,
                    }
                ],
            }
        ],
    }

    with pytest.raises(ValueError, match="must match bN.M"):
        _sanitize_and_validate_research_tree(payload)
