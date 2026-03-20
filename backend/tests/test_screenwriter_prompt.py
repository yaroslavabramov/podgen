from app.pipeline.phase6_writing.screenwriter import (
    SCREENWRITER_SYSTEM,
    _build_scene_prompt_payload,
)


def test_build_scene_prompt_payload_carries_act_contract_and_scene_goal() -> None:
    payload = _build_scene_prompt_payload(
        act={
            "title": "Черная суббота и привязка",
            "story_role": "complicate",
            "purpose": "Показать цену стабильности через кризисы.",
            "internal_arc": "от паники к механизму и компромиссу",
            "opening_question": "Что происходит, когда люди перестают верить в деньги?",
            "ending_turn": "Стабильность имеет цену.",
            "scene_strategy": "паника -> механизм -> цена",
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
    )

    assert payload == {
        "language": "ru",
        "act_number": 3,
        "act_title": "Черная суббота и привязка",
        "story_role": "complicate",
        "act_purpose": "Показать цену стабильности через кризисы.",
        "internal_arc": "от паники к механизму и компромиссу",
        "opening_question": "Что происходит, когда люди перестают верить в деньги?",
        "ending_turn": "Стабильность имеет цену.",
        "scene_strategy": "паника -> механизм -> цена",
        "scene_number": 1,
        "scene_type": "case_study",
        "description": "Черная суббота 1983 года как момент потери доверия.",
        "transition_note": "После паники перейти к механике привязки.",
        "notes": "Один ведущий начинает с конкретной паники, второй расширяет рамку.",
        "estimated_duration_seconds": 75,
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


def test_screenwriter_prompt_demands_language_lock_and_uses_act_contract() -> None:
    assert "All dialogue lines MUST be in the `language` from the user payload." in SCREENWRITER_SYSTEM
    assert "Use the act contract (`story_role`, `act_purpose`, `internal_arc`, `scene_strategy`)" in SCREENWRITER_SYSTEM
    assert "Use `description` and `transition_note` as scene goals" in SCREENWRITER_SYSTEM
    assert "Do not make Host A always explain and Host B always ask for proof in the same rhythm." in SCREENWRITER_SYSTEM
    assert "`takeaway`: land the consequence or implication, not just a generic recap" in SCREENWRITER_SYSTEM
