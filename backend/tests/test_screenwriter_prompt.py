import json

from app.config import settings
from app.pipeline.phase6_writing.screenwriter import (
    SCREENWRITER_SYSTEM,
    _build_already_covered_claims_payload,
    _build_previous_scene_payload,
    _build_scene_prompt_payload,
    _dump_screenwriter_debug,
    _extract_dialogue_claim_ids,
)


def test_build_scene_prompt_payload_uses_local_context_and_scene_goal() -> None:
    payload = _build_scene_prompt_payload(
        act={
            "title": "Черная суббота и привязка",
        },
        act_number=3,
        scene={
            "type": "case_study",
            "description": "Черная суббота 1983 года как момент потери доверия.",
            "transition_note": "После паники перейти к механике привязки.",
            "notes": "Один ведущий начинает с конкретной паники, второй расширяет рамку.",
            "estimated_duration_seconds": 75,
        },
        scene_number=1,
        scene_claims=[
            {
                "id": "c1",
                "type": "fact",
                "verification": "verified",
                "statement": "The peg was restored in October 1983.",
                "attribution": None,
            }
        ],
        scene_evidence=[
            {
                "id": "e1",
                "quote": "On 17 October 1983 the exchange rate was fixed at HK$7.8 to US$1.",
                "asset_type": "timeline",
                "tellability": "Anchor the panic in a concrete date.",
            }
        ],
        hosts=[{"name": "Host A", "role": "Guide"}, {"name": "Host B", "role": "Challenger"}],
        forbidden_phrases=["game changer"],
        max_reply_tokens=180,
        language="ru",
        previous_scene={
            "scene_number": 0,
            "scene_type": "hook",
            "description": "В предыдущей сцене объяснили, как появился валютный хаос.",
            "transition_note": "Теперь показать, что паника заставила систему измениться.",
        },
        already_covered_claims=[
            {"id": "c0", "statement": "Earlier scene already established the monetary chaos."}
        ],
    )

    assert payload == {
        "language": "ru",
        "act_number": 3,
        "act_title": "Черная суббота и привязка",
        "scene_number": 1,
        "scene_type": "case_study",
        "description": "Черная суббота 1983 года как момент потери доверия.",
        "transition_note": "После паники перейти к механике привязки.",
        "notes": "Один ведущий начинает с конкретной паники, второй расширяет рамку.",
        "estimated_duration_seconds": 75,
        "previous_scene": {
            "scene_number": 0,
            "scene_type": "hook",
            "description": "В предыдущей сцене объяснили, как появился валютный хаос.",
            "transition_note": "Теперь показать, что паника заставила систему измениться.",
        },
        "already_covered_claims": [
            {"id": "c0", "statement": "Earlier scene already established the monetary chaos."}
        ],
        "claims": [
            {
                "id": "c1",
                "type": "fact",
                "verification": "verified",
                "statement": "The peg was restored in October 1983.",
                "attribution": None,
            }
        ],
        "evidence": [
            {
                "id": "e1",
                "quote": "On 17 October 1983 the exchange rate was fixed at HK$7.8 to US$1.",
                "asset_type": "timeline",
                "tellability": "Anchor the panic in a concrete date.",
            }
        ],
        "hosts": [{"name": "Host A", "role": "Guide"}, {"name": "Host B", "role": "Challenger"}],
        "forbidden_phrases": ["game changer"],
        "max_reply_tokens": 180,
    }


def test_screenwriter_prompt_demands_language_lock_and_anti_repetition_local_memory() -> None:
    assert "All dialogue lines MUST be in the `language` from the user payload." in SCREENWRITER_SYSTEM
    assert "Use `description` as the main job of the scene." in SCREENWRITER_SYSTEM
    assert "Use `act_title`, `previous_scene`, and `already_covered_claims` only as local context." in SCREENWRITER_SYSTEM
    assert "Prefer callbacks over re-explanations." in SCREENWRITER_SYSTEM
    assert "Do not make Host A always explain and Host B always ask for proof in the same rhythm." in SCREENWRITER_SYSTEM
    assert "`takeaway`: land the consequence or implication, not just a generic recap" in SCREENWRITER_SYSTEM


def test_build_previous_scene_payload_returns_previous_scene_only() -> None:
    beat_sheet = [
        {"scene": 1, "type": "hook", "description": "Scene one", "transition_note": "Move to scene two"},
        {"scene": 2, "type": "explanation", "description": "Scene two", "transition_note": "Move to scene three"},
    ]

    assert _build_previous_scene_payload(beat_sheet, 1) is None
    assert _build_previous_scene_payload(beat_sheet, 2) == {
        "scene_number": 1,
        "scene_type": "hook",
        "description": "Scene one",
        "transition_note": "Move to scene two",
    }


def test_build_already_covered_claims_payload_uses_written_claim_ids() -> None:
    claim_lookup = {
        "c1": {"statement": "First"},
        "c2": {"statement": "Second"},
        "c3": {"statement": "Third"},
    }
    prior_dialogue = [
        {"speaker": "A", "text": "One", "claim_ids": ["c1"]},
        {"speaker": "B", "text": "Two", "claim_ids": []},
    ]

    assert _build_already_covered_claims_payload(
        covered_claim_ids=_extract_dialogue_claim_ids(prior_dialogue),
        claim_lookup=claim_lookup,
    ) == [
        {"id": "c1", "statement": "First"},
    ]


def test_extract_dialogue_claim_ids_dedupes_and_preserves_order() -> None:
    dialogue = [
        {"speaker": "A", "text": "One", "claim_ids": ["c1", "c2"]},
        {"speaker": "B", "text": "Two", "claim_ids": []},
        {"speaker": "A", "text": "Three", "claim_ids": ["c2", "c3"]},
        {"speaker": "B", "text": "Four"},
        {"speaker": "A", "text": "Five", "claim_ids": ["c1", "c4"]},
    ]

    assert _extract_dialogue_claim_ids(dialogue) == ["c1", "c2", "c3", "c4"]


def test_dump_screenwriter_debug_writes_scene_dump(tmp_path) -> None:
    original_dir = settings.web_extractor_debug_dump_dir
    settings.web_extractor_debug_dump_dir = str(tmp_path)
    try:
        _dump_screenwriter_debug(
            run_id="run-123",
            act_number=2,
            scene_number=4,
            status="ok",
            prompt_payload={"scene_type": "explanation"},
            attempts=[{"attempt": 1, "status": "ok"}],
            dialogue=[{"speaker": "A", "text": "Пример"}],
            error=None,
        )
        dump_files = list((tmp_path / "screenwriter").glob("*.json"))
        assert len(dump_files) == 1
        payload = json.loads(dump_files[0].read_text())
        assert payload["run_id"] == "run-123"
        assert payload["act_number"] == 2
        assert payload["scene_number"] == 4
        assert payload["status"] == "ok"
        assert payload["input"] == {"scene_type": "explanation"}
        assert payload["output"]["dialogue_line_count"] == 1
    finally:
        settings.web_extractor_debug_dump_dir = original_dir
