import json
from types import SimpleNamespace

from app.config import settings
from app.pipeline.phase6_writing.dialogue_utils import normalize_dialogue_output
from app.pipeline.phase6_writing.screenwriter import (
    SCREENWRITER_SYSTEM,
    _apply_screenwriter_dialogue,
    _build_speaker_aliases,
    _build_already_covered_claims_payload,
    _build_prior_scenes_dialogue,
    _build_recent_scene_starts,
    _extract_last_dialogue_line,
    _build_previous_act_summary,
    _build_previous_act_tail_dialogue,
    _build_previous_scene_payload,
    _build_scene_prompt_payload,
    _collect_manifest_claim_ids,
    _dump_screenwriter_debug,
    _extract_dialogue_claim_ids,
    _extract_dialogue_texts,
    _extract_dialogue_opener,
    _scene_manifest_claim_ids,
    _scene_callback_claim_ids,
    _scene_claim_mode,
    _scene_fingerprint,
    _normalize_speaker_label,
    _scene_job,
    _segment_scene_fingerprint,
    _scene_primary_claim_ids,
    _should_reuse_existing_scene,
    _should_force_rewrite_for_node_rerun,
)
from app.models.script_segment import AuditStatus


def _scene_prompt_payload_for_fingerprint(**overrides) -> dict:
    payload = {
        "language": "ru",
        "act_number": 2,
        "act_title": "Explain",
        "scene_number": 2,
        "scene_type": "explanation",
        "scene_claim_mode": "primary",
        "scene_job": "",
        "callback_claims": [],
        "description": "Same scene brief",
        "transition_note": "",
        "notes": "",
        "estimated_duration_seconds": 60,
        "previous_scene": None,
        "previous_act_summary": None,
        "previous_act_tail_dialogue": None,
        "prior_scenes_dialogue": [],
        "already_covered_claims": [],
        "already_covered_claims_global": [],
        "recent_scene_starts": [],
        "claims": [],
        "evidence": [],
        "hosts": [],
        "forbidden_phrases": [],
        "max_reply_tokens": 120,
    }
    payload.update(overrides)
    return payload


def test_build_scene_prompt_payload_uses_local_and_cross_act_context() -> None:
    act_claim_lookup = {
        "c1": {
            "id": "c1",
            "type": "fact",
            "verification": "verified",
            "statement": "The peg was restored in October 1983.",
            "attribution": None,
        },
    }
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
            "what_listener_now_knows": "В предыдущей сцене объяснили, как появился валютный хаос.",
            "claim_ids": ["c0"],
            "last_line": "Теперь показать, что паника заставила систему измениться.",
        },
        previous_act_summary={
            "act_number": 2,
            "act_title": "Как устроен механизм",
            "ending_scene_type": "takeaway",
            "what_listener_now_knows": "Теперь слушатель понимает, почему банки не могут печатать без ограничений.",
            "bridge_to_current_act": "Но этот механизм пришлось строить в панике 1983 года.",
        },
        previous_act_tail_dialogue=[
            {
                "scene_number": 5,
                "scene_type": "takeaway",
                "lines": [
                    {"speaker": "A", "text": "Это не доверие к человеку, а жёсткая конструкция."},
                    {"speaker": "B", "text": "Тогда вопрос уже не как это работает, а откуда это взялось."},
                ],
            }
        ],
        already_covered_claims=[
            {"id": "c0", "statement": "Earlier scene already established the monetary chaos."}
        ],
        already_covered_claims_global=[
            {
                "id": "g1",
                "statement": "Earlier act already established the reserve discipline.",
            }
        ],
        recent_scene_starts=["подожди ты", "и вот"],
        prior_scenes_dialogue=[],
        act_claim_lookup=act_claim_lookup,
    )

    assert payload == {
        "language": "ru",
        "act_number": 3,
        "act_title": "Черная суббота и привязка",
        "scene_number": 1,
        "scene_type": "case_study",
        "scene_claim_mode": "primary",
        "scene_job": "",
        "callback_claims": [],
        "description": "Черная суббота 1983 года как момент потери доверия.",
        "transition_note": "После паники перейти к механике привязки.",
        "notes": "Один ведущий начинает с конкретной паники, второй расширяет рамку.",
        "estimated_duration_seconds": 75,
        "previous_scene": {
            "scene_number": 0,
            "scene_type": "hook",
            "what_listener_now_knows": "В предыдущей сцене объяснили, как появился валютный хаос.",
            "claim_ids": ["c0"],
            "last_line": "Теперь показать, что паника заставила систему измениться.",
        },
        "previous_act_summary": {
            "act_number": 2,
            "act_title": "Как устроен механизм",
            "ending_scene_type": "takeaway",
            "what_listener_now_knows": "Теперь слушатель понимает, почему банки не могут печатать без ограничений.",
            "bridge_to_current_act": "Но этот механизм пришлось строить в панике 1983 года.",
        },
        "previous_act_tail_dialogue": [
            {
                "scene_number": 5,
                "scene_type": "takeaway",
                "lines": [
                    {"speaker": "A", "text": "Это не доверие к человеку, а жёсткая конструкция."},
                    {"speaker": "B", "text": "Тогда вопрос уже не как это работает, а откуда это взялось."},
                ],
            }
        ],
        "prior_scenes_dialogue": [],
        "already_covered_claims": [
            {"id": "c0", "statement": "Earlier scene already established the monetary chaos."}
        ],
        "already_covered_claims_global": [
            {
                "id": "g1",
                "statement": "Earlier act already established the reserve discipline.",
            }
        ],
        "recent_scene_starts": ["подожди ты", "и вот"],
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


def test_screenwriter_prompt_demands_cross_act_memory_and_looser_dialogue() -> None:
    assert "All dialogue lines MUST be in the `language` from the user payload." in SCREENWRITER_SYSTEM
    assert "Keep the spoken register natural for that language; avoid translated-from-English phrasing." in SCREENWRITER_SYSTEM
    assert "If evidence, quotes, or source material are in another language" in SCREENWRITER_SYSTEM
    assert "translate or paraphrase them into the dialogue language" in SCREENWRITER_SYSTEM
    assert "The `speaker` field must use canonical IDs `A` or `B` only." in SCREENWRITER_SYSTEM
    assert "Never output host names, role names, localized speaker labels, or other aliases" in SCREENWRITER_SYSTEM
    assert "Use `description` as the main job of the scene." in SCREENWRITER_SYSTEM
    assert "previous_act_summary" in SCREENWRITER_SYSTEM
    assert "previous_act_tail_dialogue" in SCREENWRITER_SYSTEM
    assert "already_covered_claims_global" in SCREENWRITER_SYSTEM
    assert "prior_scenes_dialogue" in SCREENWRITER_SYSTEM
    assert "continue the same conversation instead of resetting the story from zero" in SCREENWRITER_SYSTEM
    assert "Do not open a new act by stepping back to a more basic pedagogical question" in SCREENWRITER_SYSTEM
    assert "Prefer callbacks over re-explanations." in SCREENWRITER_SYSTEM
    assert "Do not default every scene to strict `ABAB` alternation." in SCREENWRITER_SYSTEM
    assert "The first 1-2 lines of a new scene should advance, challenge, narrow, or reframe" in SCREENWRITER_SYSTEM
    assert "If the previous scene already announced the event, mechanism result, or punchline" in SCREENWRITER_SYSTEM
    assert "For `cost` scenes, open with the price, contradiction, or compromise being paid." in SCREENWRITER_SYSTEM
    assert "Vary agreement, confirmation, and challenge shapes." in SCREENWRITER_SYSTEM
    assert "A direct continuation, answer, or correction is often better than an explicit confirmation line." in SCREENWRITER_SYSTEM
    assert "A challenge can begin from tension, contrast, doubt, or a missing implication." in SCREENWRITER_SYSTEM
    assert "If a nearby scene already used the same opener shape or agreement shape" in SCREENWRITER_SYSTEM
    assert "Do not default every reaction to the same opener or confirmation shape." in SCREENWRITER_SYSTEM
    assert "Contrastive opener guidance:" in SCREENWRITER_SYSTEM
    assert 'Bad: scene N ends with "so the defense worked"' in SCREENWRITER_SYSTEM
    assert "Better: previous scene lands the conclusion, new scene opens from the cost" in SCREENWRITER_SYSTEM
    assert "These are soft tendencies, not rigid scene roles." in SCREENWRITER_SYSTEM
    assert "scene_claim_mode" in SCREENWRITER_SYSTEM
    assert "scene_job" in SCREENWRITER_SYSTEM
    assert "callback_claims" in SCREENWRITER_SYSTEM
    assert "B does not have to contribute mainly through questions." in SCREENWRITER_SYSTEM
    assert "Do not default to `B asks -> A confirms -> A explains`." in SCREENWRITER_SYSTEM
    assert "previous_scene.last_line" in SCREENWRITER_SYSTEM
    assert "recent_scene_starts" in SCREENWRITER_SYSTEM
    assert "takeaway" in SCREENWRITER_SYSTEM
    assert "recontextualize the act" in SCREENWRITER_SYSTEM
    assert "When a precise figure, legal clause, institutional rule, or date matters" in SCREENWRITER_SYSTEM
    assert "Do not dump raw quotes or source language into dialogue unless quoting the wording itself is the point of the beat." in SCREENWRITER_SYSTEM
    assert "Host B must contribute substance, not just react." in SCREENWRITER_SYSTEM
    assert "B's pushback should be specific and grounded" in SCREENWRITER_SYSTEM
    assert "if any confirmation word, filler, or discourse marker has already appeared twice" in SCREENWRITER_SYSTEM
    assert "Vary how lines begin." in SCREENWRITER_SYSTEM
    assert "scan `prior_scenes_dialogue` for recurring opener words" in SCREENWRITER_SYSTEM


def test_build_previous_scene_payload_prefers_actual_written_scene_continuity() -> None:
    beat_sheet = [
        {
            "scene": 1,
            "type": "hook",
            "description": "Scene one",
            "transition_note": "Move to scene two",
            "claim_ids": ["c1"],
        },
        {"scene": 2, "type": "explanation", "description": "Scene two", "transition_note": "Move to scene three"},
    ]
    existing_segments = {
        (3, 1): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "We already established the reserve rule.", "claim_ids": ["c1"]},
                {"speaker": "B", "text": "So now show what broke first.", "claim_ids": []},
            ]
        )
    }
    claim_lookup = {
        "c1": {"statement": "The reserve rule was already established."}
    }

    assert _build_previous_scene_payload(
        beat_sheet,
        1,
        act_number=3,
        existing_segments=existing_segments,
        claim_lookup=claim_lookup,
    ) is None
    assert _build_previous_scene_payload(
        beat_sheet,
        2,
        act_number=3,
        existing_segments=existing_segments,
        claim_lookup=claim_lookup,
    ) == {
        "scene_number": 1,
        "scene_type": "hook",
        "what_listener_now_knows": "The reserve rule was already established.",
        "claim_ids": ["c1"],
        "last_line": "So now show what broke first.",
    }


def test_apply_screenwriter_dialogue_resets_quality_and_updates_baseline() -> None:
    segment = SimpleNamespace(
        dialogue_json=[{"speaker": "A", "text": "Old line", "claim_ids": ["c0"]}],
        writer_original_dialogue_json=[{"speaker": "A", "text": "Older baseline", "claim_ids": ["c-1"]}],
        engagement_score=0.5,
        audit_status="failed",
        revision_count=2,
        quality_metrics={"fact_coverage_ratio": 0.5},
        audit_findings={"citation": {"issues": []}},
    )
    new_dialogue = [{"speaker": "B", "text": "New writer draft", "claim_ids": ["c1"]}]

    _apply_screenwriter_dialogue(segment, new_dialogue)

    assert segment.dialogue_json == new_dialogue
    assert segment.writer_original_dialogue_json == new_dialogue
    assert segment.dialogue_json is not new_dialogue
    assert segment.writer_original_dialogue_json is not new_dialogue
    assert segment.engagement_score is None
    assert segment.audit_status == "pending"
    assert segment.revision_count == 0
    assert segment.quality_metrics is None
    assert segment.audit_findings is None


def test_apply_screenwriter_dialogue_stores_scene_fingerprint_metadata() -> None:
    segment = SimpleNamespace(
        dialogue_json=[],
        writer_original_dialogue_json=[],
        engagement_score=None,
        audit_status="failed",
        revision_count=1,
        quality_metrics={"fact_coverage_ratio": 0.5},
        audit_findings={"citation": {"issues": []}},
    )

    _apply_screenwriter_dialogue(
        segment,
        [{"speaker": "A", "text": "Fresh draft", "claim_ids": ["c1"]}],
        scene_fingerprint="fingerprint-123",
    )

    assert segment.audit_findings == {
        "screenwriter": {
            "scene_fingerprint": "fingerprint-123",
        }
    }
    assert segment.revision_count == 0


def test_segment_scene_fingerprint_reads_screenwriter_metadata() -> None:
    segment = SimpleNamespace(
        audit_findings={
            "screenwriter": {"scene_fingerprint": "fingerprint-123"},
            "citation": {"issues": []},
        }
    )

    assert _segment_scene_fingerprint(segment) == "fingerprint-123"


def test_scene_fingerprint_changes_when_scene_brief_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=4,
            act_title="Stress test",
            scene_number=3,
            scene_type="case_study",
            scene_job="apply",
            description="Original description",
            transition_note="Bridge",
            notes="Note",
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=4,
            act_title="Stress test",
            scene_number=3,
            scene_type="case_study",
            scene_job="apply",
            description="Changed description",
            transition_note="Bridge",
            notes="Note",
        ),
    )


def test_scene_fingerprint_changes_when_previous_scene_context_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=5,
            act_title="Payoff",
            scene_number=1,
            previous_scene={"last_line": "старый финал"},
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=5,
            act_title="Payoff",
            scene_number=1,
            previous_scene={"last_line": "новый финал"},
        ),
    )


def test_scene_fingerprint_changes_when_previous_act_summary_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=5,
            act_title="Payoff",
            scene_number=1,
            previous_act_summary={"what_listener_now_knows": "old summary"},
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=5,
            act_title="Payoff",
            scene_number=1,
            previous_act_summary={"what_listener_now_knows": "new summary"},
        ),
    )


def test_scene_fingerprint_changes_when_system_prompt_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=5,
            act_title="Payoff",
            scene_number=1,
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v2",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            act_number=5,
            act_title="Payoff",
            scene_number=1,
        ),
    )


def test_scene_fingerprint_changes_when_scene_claim_payload_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            claims=[{"id": "c1", "statement": "old statement", "verification": "verified"}],
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            claims=[{"id": "c1", "statement": "new statement", "verification": "verified"}],
        ),
    )


def test_scene_fingerprint_changes_when_scene_evidence_payload_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            evidence=[{"id": "e1", "quote": "old quote", "asset_type": "mechanism", "tellability": "old note"}],
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            evidence=[{"id": "e1", "quote": "new quote", "asset_type": "mechanism", "tellability": "old note"}],
        ),
    )


def test_scene_fingerprint_changes_when_callback_claim_payload_changes() -> None:
    assert _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            scene_claim_mode="mixed",
            callback_claims=[{"id": "c2", "statement": "old callback", "verification": "verified"}],
        ),
    ) != _scene_fingerprint(
        system_prompt="system-v1",
        prompt_payload=_scene_prompt_payload_for_fingerprint(
            scene_claim_mode="mixed",
            callback_claims=[{"id": "c2", "statement": "new callback", "verification": "verified"}],
        ),
    )


def test_should_not_reuse_existing_scene_without_matching_fingerprint() -> None:
    current_scene_fingerprint = "new-fingerprint"
    existing_segment = SimpleNamespace(
        dialogue_json=[{"speaker": "A", "text": "Old line"}],
        audit_status=AuditStatus.PASSED,
        audit_findings={},
    )

    assert not _should_reuse_existing_scene(
        existing_segment,
        current_scene_fingerprint=current_scene_fingerprint,
        existing_line_count=1,
        force_rewrite_on_step_rerun=False,
    )


def test_should_not_reuse_existing_scene_when_fingerprint_mismatch() -> None:
    existing_segment = SimpleNamespace(
        dialogue_json=[{"speaker": "A", "text": "Old line"}],
        audit_status=AuditStatus.PASSED,
        audit_findings={"screenwriter": {"scene_fingerprint": "old-fingerprint"}},
    )

    assert not _should_reuse_existing_scene(
        existing_segment,
        current_scene_fingerprint="new-fingerprint",
        existing_line_count=1,
        force_rewrite_on_step_rerun=False,
    )


def test_should_reuse_existing_scene_when_fingerprint_matches() -> None:
    existing_segment = SimpleNamespace(
        dialogue_json=[{"speaker": "A", "text": "Old line"}],
        audit_status=AuditStatus.PASSED,
        audit_findings={"screenwriter": {"scene_fingerprint": "same-fingerprint"}},
    )

    assert _should_reuse_existing_scene(
        existing_segment,
        current_scene_fingerprint="same-fingerprint",
        existing_line_count=1,
        force_rewrite_on_step_rerun=False,
    )


def test_should_not_reuse_existing_scene_on_targeted_rerun_even_when_fingerprint_matches() -> None:
    existing_segment = SimpleNamespace(
        dialogue_json=[{"speaker": "A", "text": "Old line"}],
        audit_status=AuditStatus.PASSED,
        audit_findings={"screenwriter": {"scene_fingerprint": "same-fingerprint"}},
    )

    assert not _should_reuse_existing_scene(
        existing_segment,
        current_scene_fingerprint="same-fingerprint",
        existing_line_count=1,
        force_rewrite_on_step_rerun=True,
    )


def test_build_previous_scene_payload_falls_back_to_manifest_claims_and_transition_note() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    beat_sheet = [
        {
            "scene": 1,
            "type": "explanation",
            "description": "Explain the rule, the institutional constraint, and the legal wrapper around it.",
            "transition_note": "Now pressure-test that rule.",
            "claim_ids": [c1, c2],
        },
        {"scene": 2, "type": "case_study", "description": "Scene two"},
    ]
    claim_lookup = {
        c1: {"statement": "The peg runs through a reserve requirement."},
        c2: {"statement": "The legal wrapper constrains how issuance can expand."},
    }

    assert _build_previous_scene_payload(
        beat_sheet,
        2,
        act_number=3,
        existing_segments={},
        claim_lookup=claim_lookup,
    ) == {
        "scene_number": 1,
        "scene_type": "explanation",
        "what_listener_now_knows": (
            "The peg runs through a reserve requirement.; "
            "The legal wrapper constrains how issuance can expand."
        ),
        "claim_ids": [c1, c2],
        "last_line": "Now pressure-test that rule.",
    }


def test_build_previous_act_summary_returns_previous_act_takeaway() -> None:
    acts_json = [
        {
            "act_number": 1,
            "title": "Origins",
            "ending_turn": "By the end of the act, the listener understands why private banks stepped in.",
            "beat_sheet": [
                {"scene": 1, "type": "hook", "description": "Opening", "transition_note": "Move on"},
                {
                    "scene": 2,
                    "type": "takeaway",
                    "description": "Now the listener knows the port was chaotic.",
                    "transition_note": "Next explain why private banks stepped in.",
                },
            ],
        },
        {"act_number": 2, "title": "Mechanism", "beat_sheet": []},
    ]

    assert _build_previous_act_summary(acts_json, 1, existing_segments={}, claim_lookup={}) is None
    assert _build_previous_act_summary(acts_json, 2, existing_segments={}, claim_lookup={}) == {
        "act_number": 1,
        "act_title": "Origins",
        "ending_scene_type": "takeaway",
        "what_listener_now_knows": "By the end of the act, the listener understands why private banks stepped in.",
        "bridge_to_current_act": "Next explain why private banks stepped in.",
    }


def test_build_previous_act_summary_prefers_written_dialogue_over_planning_metadata() -> None:
    acts_json = [
        {
            "act_number": 1,
            "title": "Origins",
            "ending_turn": "Planning-only ending turn.",
            "beat_sheet": [
                {
                    "scene": 1,
                    "type": "takeaway",
                    "description": "Planning-only description.",
                    "transition_note": "Planning-only transition note.",
                }
            ],
        },
        {"act_number": 2, "title": "Mechanism", "beat_sheet": []},
    ]
    existing_segments = {
        (1, 1): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "We already learned that private banks stepped in.", "claim_ids": ["c1"]},
                {"speaker": "B", "text": "And that is the bridge to the mechanism.", "claim_ids": []},
            ]
        )
    }
    claim_lookup = {"c1": {"statement": "Private banks stepped in because the state would not emit directly."}}

    assert _build_previous_act_summary(
        acts_json,
        2,
        existing_segments=existing_segments,
        claim_lookup=claim_lookup,
    ) == {
        "act_number": 1,
        "act_title": "Origins",
        "ending_scene_type": "takeaway",
        "what_listener_now_knows": "Private banks stepped in because the state would not emit directly.",
        "bridge_to_current_act": "And that is the bridge to the mechanism.",
    }


def test_build_previous_act_tail_dialogue_prefers_written_tail_of_previous_act() -> None:
    acts_json = [
        {
            "act_number": 1,
            "title": "Origins",
            "beat_sheet": [
                {"scene": 1, "type": "hook"},
                {"scene": 2, "type": "explanation"},
                {"scene": 3, "type": "takeaway"},
            ],
        },
        {"act_number": 2, "title": "Mechanism", "beat_sheet": []},
    ]
    existing_segments = {
        (1, 1): SimpleNamespace(dialogue_json=[{"speaker": "A", "text": "Old hook."}]),
        (1, 2): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "Mechanism line one."},
                {"speaker": "B", "text": "Mechanism line two."},
            ]
        ),
        (1, 3): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "Takeaway line one."},
                {"speaker": "B", "text": "Takeaway line two."},
                {"speaker": "A", "text": "Takeaway line three."},
                {"speaker": "B", "text": "Takeaway line four."},
                {"speaker": "A", "text": "Takeaway line five."},
            ]
        ),
    }

    assert _build_previous_act_tail_dialogue(acts_json, 1, existing_segments=existing_segments) == []
    assert _build_previous_act_tail_dialogue(acts_json, 2, existing_segments=existing_segments) == [
        {
            "scene_number": 2,
            "scene_type": "explanation",
            "lines": [
                {"speaker": "A", "text": "Mechanism line one."},
                {"speaker": "B", "text": "Mechanism line two."},
            ],
        },
        {
            "scene_number": 3,
            "scene_type": "takeaway",
            "lines": [
                {"speaker": "B", "text": "Takeaway line two."},
                {"speaker": "A", "text": "Takeaway line three."},
                {"speaker": "B", "text": "Takeaway line four."},
                {"speaker": "A", "text": "Takeaway line five."},
            ],
        },
    ]


def test_build_previous_act_tail_dialogue_skips_non_list_dialogue_json() -> None:
    acts_json = [
        {
            "act_number": 1,
            "title": "Origins",
            "beat_sheet": [
                {"scene": 1, "type": "hook"},
                {"scene": 2, "type": "takeaway"},
            ],
        },
        {"act_number": 2, "title": "Mechanism", "beat_sheet": []},
    ]
    existing_segments = {
        (1, 1): SimpleNamespace(dialogue_json=[{"speaker": "A", "text": "Still valid."}]),
        (1, 2): SimpleNamespace(dialogue_json=None),
    }

    assert _build_previous_act_tail_dialogue(acts_json, 2, existing_segments=existing_segments) == [
        {
            "scene_number": 1,
            "scene_type": "hook",
            "lines": [{"speaker": "A", "text": "Still valid."}],
        }
    ]


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


def test_build_already_covered_claims_payload_can_cap_and_truncate_for_global_memory() -> None:
    claim_lookup = {
        "c1": {"statement": "First statement that should be dropped by the cap."},
        "c2": {"statement": "Second statement that should remain."},
        "c3": {"statement": "Third statement that should remain and be truncated if needed."},
    }

    assert _build_already_covered_claims_payload(
        covered_claim_ids=["c1", "c2", "c3"],
        claim_lookup=claim_lookup,
        max_items=2,
        statement_chars=10,
    ) == [
        {"id": "c2", "statement": "Second sta"},
        {"id": "c3", "statement": "Third stat"},
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


def test_extract_last_dialogue_line_returns_last_nonempty_text() -> None:
    assert _extract_last_dialogue_line(
        [
            {"speaker": "A", "text": "First"},
            {"speaker": "B", "text": "  "},
            {"speaker": "A", "text": "Last meaningful line"},
        ]
    ) == "Last meaningful line"


def test_collect_manifest_claim_ids_dedupes_in_order() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    c3 = "33333333-3333-3333-3333-333333333333"
    c4 = "44444444-4444-4444-4444-444444444444"
    acts_json = [
        {
            "beat_sheet": [
                {"primary_claim_ids": [c1], "callback_claim_ids": [c2]},
                {"claim_ids": [c2, c3]},
            ]
        },
        {"beat_sheet": [{"primary_claim_ids": [c3], "callback_claim_ids": [c4]}]},
    ]

    assert _collect_manifest_claim_ids(acts_json) == [c1, c2, c3, c4]


def test_scene_claim_helpers_prefer_primary_and_include_callbacks_for_manifest() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    c3 = "33333333-3333-3333-3333-333333333333"
    scene = {
        "primary_claim_ids": [c1],
        "callback_claim_ids": [c2],
        "claim_ids": [c1, c2, c3],
    }

    assert _scene_primary_claim_ids(scene) == [c1]
    assert _scene_callback_claim_ids(scene) == [c2]
    assert _scene_claim_mode(scene) == "mixed"
    assert _scene_manifest_claim_ids(scene) == [c1, c2, c3]


def test_scene_claim_mode_defaults_to_callback_or_primary() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"

    assert _scene_claim_mode({"callback_claim_ids": [c1]}) == "callback"
    assert _scene_claim_mode({"claim_ids": [c1]}) == "primary"
    assert _scene_job({"callback_claim_ids": [c1], "type": "takeaway"}) == "synthesize"
    assert _scene_job({"callback_claim_ids": [c1], "type": "transition"}) == "bridge"


def test_build_scene_prompt_payload_marks_callback_only_scene() -> None:
    c1 = "11111111-1111-1111-1111-111111111111"
    act_claim_lookup = {
        c1: {"id": c1, "type": "fact", "verification": "verified", "statement": "Callback claim.", "attribution": None},
    }

    payload = _build_scene_prompt_payload(
        act={"title": "Payoff"},
        act_number=4,
        scene={
            "type": "takeaway",
            "scene_job": "synthesize",
            "callback_claim_ids": [c1],
            "description": "Land the implication without reteaching the mechanism.",
            "transition_note": "",
            "notes": "",
            "estimated_duration_seconds": 45,
        },
        scene_number=6,
        scene_claims=[],
        scene_evidence=[],
        hosts=[],
        forbidden_phrases=[],
        max_reply_tokens=120,
        language="ru",
        previous_scene=None,
        previous_act_summary=None,
        previous_act_tail_dialogue=None,
        already_covered_claims=[],
        already_covered_claims_global=[],
        recent_scene_starts=["подожди ты"],
        prior_scenes_dialogue=[],
        act_claim_lookup=act_claim_lookup,
    )

    assert payload["scene_claim_mode"] == "callback"
    assert payload["scene_job"] == "synthesize"
    assert payload["callback_claims"] == [act_claim_lookup[c1]]
    assert payload["recent_scene_starts"] == ["подожди ты"]


def test_build_prior_scenes_dialogue_returns_speaker_text_only() -> None:
    beat_sheet = [
        {"scene": 1, "type": "hook"},
        {"scene": 2, "type": "explanation"},
        {"scene": 3, "type": "case_study"},
    ]
    existing_segments = {
        (2, 1): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "Opening hook.", "claim_ids": ["c1"], "ssml_hints": {}, "emotion": "curious"},
                {"speaker": "B", "text": "Good point.", "claim_ids": [], "ssml_hints": {}, "emotion": "serious"},
            ]
        ),
        (2, 2): SimpleNamespace(
            dialogue_json=[
                {"speaker": "A", "text": "Now explain.", "claim_ids": ["c2"]},
            ]
        ),
    }

    result = _build_prior_scenes_dialogue(beat_sheet, 3, act_number=2, existing_segments=existing_segments)

    assert len(result) == 2
    assert result[0] == {
        "scene_number": 1,
        "scene_type": "hook",
        "lines": [
            {"speaker": "A", "text": "Opening hook."},
            {"speaker": "B", "text": "Good point."},
        ],
    }
    assert result[1] == {
        "scene_number": 2,
        "scene_type": "explanation",
        "lines": [
            {"speaker": "A", "text": "Now explain."},
        ],
    }


def test_build_prior_scenes_dialogue_empty_for_first_scene() -> None:
    beat_sheet = [{"scene": 1, "type": "hook"}]
    result = _build_prior_scenes_dialogue(beat_sheet, 1, act_number=1, existing_segments={})
    assert result == []


def test_extract_dialogue_texts_returns_stripped_nonempty() -> None:
    dialogue = [
        {"speaker": "A", "text": "  The peg sits at 7.80.  "},
        {"speaker": "B", "text": "HKMA enforces it."},
        {"speaker": "A", "text": ""},
    ]

    texts = _extract_dialogue_texts(dialogue)
    assert texts == ["The peg sits at 7.80.", "HKMA enforces it."]


def test_extract_dialogue_opener_normalizes_first_two_words() -> None:
    dialogue = [
        {"speaker": "A", "text": "Подожди — ты говоришь, что система сама себя удерживает?"},
        {"speaker": "B", "text": "Да."},
    ]

    assert _extract_dialogue_opener(dialogue) == "подожди ты"


def test_normalize_speaker_label_maps_cyrillic_and_named_hosts() -> None:
    aliases = _build_speaker_aliases(
        [
            {"name": "Марина", "role": "Guide"},
            {"name": "Дима Ковалев", "role": "Challenger"},
        ]
    )

    assert _normalize_speaker_label("A") == "A"
    assert _normalize_speaker_label("Б") == "B"
    assert _normalize_speaker_label("Host A") == "A"
    assert _normalize_speaker_label("Host B:") == "B"
    assert _normalize_speaker_label("speaker b") == "B"
    assert _normalize_speaker_label("Speaker B:") == "B"
    assert _normalize_speaker_label("Марина", speaker_aliases=aliases) == "A"
    assert _normalize_speaker_label("марина:", speaker_aliases=aliases) == "A"
    assert _normalize_speaker_label("Дима Ковалев", speaker_aliases=aliases) == "B"
    assert _normalize_speaker_label("Дима", speaker_aliases=aliases) == "B"
    assert _normalize_speaker_label("Guide", speaker_aliases=aliases) == "A"
    assert _normalize_speaker_label("Challenger", speaker_aliases=aliases) == "B"
    assert _normalize_speaker_label("") == "A"
    assert _normalize_speaker_label(None) == "A"


def test_normalize_dialogue_output_keeps_host_b_punctuation_as_b() -> None:
    normalized = normalize_dialogue_output(
        [{"speaker": "Host B:", "text": "A valid line.", "claim_ids": []}]
    )

    assert normalized == [{"speaker": "B", "text": "A valid line.", "claim_ids": []}]


def test_build_recent_scene_starts_uses_written_previous_scenes_in_order() -> None:
    beat_sheet = [
        {"scene": 1},
        {"scene": 2},
        {"scene": 3},
    ]
    existing_segments = {
        (2, 1): SimpleNamespace(dialogue_json=[{"speaker": "A", "text": "И вот начинается механизм."}]),
        (2, 2): SimpleNamespace(dialogue_json=[{"speaker": "B", "text": "Подожди — это уже не хаос?"}]),
    }

    assert _build_recent_scene_starts(
        beat_sheet,
        3,
        act_number=2,
        existing_segments=existing_segments,
    ) == ["и вот", "подожди это"]


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


def test_should_force_rewrite_for_targeted_screenwriter_rerun() -> None:
    assert _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "rerun",
                "requested_target_node": "write_screenplay",
                "last_node": "integrate_acts",
            }
        },
        node_name="write_screenplay",
    )


def test_should_force_rewrite_for_last_node_rerun_without_explicit_target() -> None:
    assert _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "rerun",
                "requested_target_node": None,
                "last_node": "write_screenplay",
            }
        },
        node_name="write_screenplay",
    )


def test_should_force_rewrite_for_any_rerun_target() -> None:
    assert _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "rerun",
                "requested_target_node": "plan_acts",
                "last_node": "write_screenplay",
            }
        },
        node_name="write_screenplay",
    )


def test_should_not_force_rewrite_for_non_rerun() -> None:
    assert not _should_force_rewrite_for_node_rerun(
        {
            "step_control": {
                "requested_action": "next",
                "requested_target_node": "write_screenplay",
                "last_node": "write_screenplay",
            }
        },
        node_name="write_screenplay",
    )
