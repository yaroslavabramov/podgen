import json

import pytest

from app.pipeline.phase5_planning.showrunner import (
    SHOWRUNNER_PLAN_SCHEMA,
    SHOWRUNNER_REVISION_SYSTEM,
    SHOWRUNNER_SELECTION_AUDIT_MAX_TOKENS,
    SHOWRUNNER_SYSTEM,
    _align_conflict_quotas,
    _call_showrunner_generation_structured,
    _build_showrunner_prompt_payload,
    _build_showrunner_revision_payload,
    _compact_selection_act_view,
    _compute_max_claims_per_act,
    _normalize_showrunner_selection_issues,
    _normalize_plan,
    _rebalance_act_claim_distribution,
    _run_showrunner_selection_audit,
    _selected_claim_ids,
    _selection_audit_raw_signals_failure,
    _shorten_plan_for_selection_audit,
    _showrunner_selection_blockers,
    _validate_showrunner_selection_issue_ids,
    _shorten_plan_claim_ids,
    _showrunner_generation_metadata,
    plan_episode,
)
from app.pipeline.short_ids import ShortIdMap


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("unexpected DB query")

    async def commit(self):
        raise AssertionError("unexpected DB commit")


def test_normalize_plan_preserves_story_fields() -> None:
    plan = _normalize_plan(
        {
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "setup",
                    "title": "How the money got weird",
                    "purpose": "Establish the strange starting point and why the listener should care.",
                    "act_job": "turn a visual anomaly into the episode question",
                    "internal_arc": "paradox -> failed fix -> institutional turn",
                    "opening_question": "Why would a city let private banks print money?",
                    "ending_turn": "The historical oddity becomes an institutional design puzzle.",
                    "estimated_duration_seconds": 420,
                    "backbone_claim_ids": ["11111111-1111-1111-1111-111111111111"],
                    "support_claim_ids": ["22222222-2222-2222-2222-222222222222"],
                    "callback_claim_ids": ["33333333-3333-3333-3333-333333333333"],
                    "conflict_brief_ids": [],
                    "content_quotas": {},
                    "why_this_is_enough": "The act needs the anomaly and one support detail, not the whole mechanism.",
                }
            ]
        },
        duration_target=1800,
    )

    act = plan["acts"][0]
    assert act["story_role"] == "setup"
    assert act["purpose"].startswith("Establish the strange starting point")
    assert act["act_job"] == "turn a visual anomaly into the episode question"
    assert act["internal_arc"] == "paradox -> failed fix -> institutional turn"
    assert act["opening_question"] == "Why would a city let private banks print money?"
    assert act["ending_turn"] == "The historical oddity becomes an institutional design puzzle."
    assert act["backbone_claim_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert act["support_claim_ids"] == ["22222222-2222-2222-2222-222222222222"]
    assert act["callback_claim_ids"] == ["33333333-3333-3333-3333-333333333333"]
    assert act["claim_ids"] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]
    assert act["why_this_is_enough"].startswith("The act needs")


def test_normalize_plan_rejects_support_only_role_payload() -> None:
    claim_id = "11111111-1111-1111-1111-111111111111"

    with pytest.raises(RuntimeError, match="support/callback claims but no backbone"):
        _normalize_plan(
            {
                "acts": [
                    {
                        "act_number": 1,
                        "story_role": "setup",
                        "title": "Support only",
                        "backbone_claim_ids": [],
                        "support_claim_ids": [claim_id],
                        "callback_claim_ids": [],
                    }
                ]
            },
            duration_target=1800,
        )


def test_align_conflict_quotas_matches_actual_disputed_and_conflict_claims() -> None:
    clean_claim = "11111111-1111-1111-1111-111111111111"
    disputed_claim = "22222222-2222-2222-2222-222222222222"
    conflict_claim = "33333333-3333-3333-3333-333333333333"
    plan = {
        "acts": [
            {
                "act_number": 1,
                "claim_ids": [clean_claim],
                "content_quotas": {"verified_facts": 1, "conflicts": 1},
            },
            {
                "act_number": 2,
                "claim_ids": [disputed_claim],
                "content_quotas": {"conflicts": 0},
            },
            {
                "act_number": 3,
                "claim_ids": [conflict_claim],
                "content_quotas": {"conflicts": 0},
            },
        ]
    }

    aligned = _align_conflict_quotas(
        plan,
        disputed_ids=[disputed_claim],
        conflicts_summary=[{"claim_ids": [conflict_claim]}],
    )

    assert aligned["acts"][0]["content_quotas"] == {"verified_facts": 1, "conflicts": 0}
    assert aligned["acts"][1]["content_quotas"]["conflicts"] == 1
    assert aligned["acts"][2]["content_quotas"]["conflicts"] == 1


def test_showrunner_prompt_does_not_hardcode_three_acts() -> None:
    assert '"act_count": 3' not in SHOWRUNNER_SYSTEM
    assert "compress this to 3 acts" not in SHOWRUNNER_SYSTEM
    assert "do not default to 3 acts" in SHOWRUNNER_SYSTEM
    assert '"internal_arc": "short phrase describing how this act moves internally"' in SHOWRUNNER_SYSTEM
    assert '"backbone_claim_ids": ["id1"]' in SHOWRUNNER_SYSTEM
    assert '"support_claim_ids": ["id2"]' in SHOWRUNNER_SYSTEM
    assert '"callback_claim_ids": ["id3"]' in SHOWRUNNER_SYSTEM
    assert '"claim_ids": [' not in SHOWRUNNER_SYSTEM
    assert "Do not output a combined `claim_ids` list" in SHOWRUNNER_SYSTEM
    act_schema = SHOWRUNNER_PLAN_SCHEMA["properties"]["acts"]["items"]
    assert "claim_ids" not in act_schema["properties"]
    assert "claim_ids" not in act_schema["required"]
    assert "Adjacent acts should usually have different `internal_arc` and `ending_turn` shapes." in SHOWRUNNER_SYSTEM
    assert "Treat `story_role` as listener work, not topic label or fixed position." in SHOWRUNNER_SYSTEM
    assert "`episode_goal` is the user's stated intent" in SHOWRUNNER_SYSTEM
    assert "`root_question` is the episode-level question" in SHOWRUNNER_SYSTEM
    assert "Selection comes before act assignment." in SHOWRUNNER_SYSTEM
    assert "First choose the smallest coherent editorial backbone" in SHOWRUNNER_SYSTEM
    assert 'Start by imagining the episode pitch to an editor' in SHOWRUNNER_SYSTEM
    assert "then decide how many listener questions it supports" in SHOWRUNNER_SYSTEM
    assert "split selected material by airtime role" in SHOWRUNNER_SYSTEM
    assert "`backbone_claim_ids` are the claims that scenes should be built around" in SHOWRUNNER_SYSTEM
    assert "`support_claim_ids` may be used inside those scenes but should not create scenes by themselves" in SHOWRUNNER_SYSTEM
    assert "Match question type to backbone." in SHOWRUNNER_SYSTEM
    assert "A chronology-only spine answers what happened, not why" in SHOWRUNNER_SYSTEM
    assert "one main listener question" in SHOWRUNNER_SYSTEM
    assert "not from a fixed universal arc" in SHOWRUNNER_SYSTEM
    assert "Silently compare possible centers of gravity" in SHOWRUNNER_SYSTEM
    assert "Treat those centers of gravity as options, not templates." in SHOWRUNNER_SYSTEM
    assert "Any order can work if `opening_question`, `internal_arc`, `ending_turn`, and backbone claims make the listener transformation clear." in SHOWRUNNER_SYSTEM
    assert "Do not default to present-anomaly -> chronology -> mechanism -> payoff" in SHOWRUNNER_SYSTEM
    assert "not the most obvious chronological or topical order" in SHOWRUNNER_SYSTEM
    assert "why is this subject strange, how does it work, and why does it matter?" not in SHOWRUNNER_SYSTEM
    assert "All human-readable fields (`title`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`) MUST be in `output_language`" in SHOWRUNNER_SYSTEM
    assert "A major shift in timeframe, abstraction level, or editorial function is evidence for a new act" in SHOWRUNNER_SYSTEM
    assert "Merge candidate act material only when it would produce substantially the same `opening_question`, `internal_arc`, and `ending_turn`." in SHOWRUNNER_SYSTEM
    assert "Role purity beats compactness." in SHOWRUNNER_SYSTEM
    assert "For setup/hook work, pose the paradox" in SHOWRUNNER_SYSTEM
    assert "backbone should carry visible anomaly/stakes, not the answer" in SHOWRUNNER_SYSTEM
    assert "do not make answer/control/issuance-condition/reserve/peg/mechanism claims backbone" in SHOWRUNNER_SYSTEM
    assert "Foreshadow at most as support/callback so later explain/resolve acts still land the reveal" in SHOWRUNNER_SYSTEM
    assert "Before keeping a claim in an act, ask whether removing it would force a genuinely different beat." in SHOWRUNNER_SYSTEM
    assert "it is not fresh backbone" in SHOWRUNNER_SYSTEM
    assert "usually 3-7 claims" in SHOWRUNNER_SYSTEM
    assert "Treat airtime as editorial investment." in SHOWRUNNER_SYSTEM
    assert "Every minute must buy a stronger reason to care" in SHOWRUNNER_SYSTEM
    assert "Set `estimated_duration_seconds` from earned listener work, not equal blocks." in SHOWRUNNER_SYSTEM
    assert "Pure setup/hook work usually earns 90-180 seconds" in SHOWRUNNER_SYSTEM
    assert "do not pad it" in SHOWRUNNER_SYSTEM
    assert "Give surplus to the hardest mechanism" in SHOWRUNNER_SYSTEM
    assert "compress it into support for a stronger act" in SHOWRUNNER_SYSTEM
    assert "one anchor mechanism, event, example, or conflict" in SHOWRUNNER_SYSTEM
    assert "A backbone claim earns airtime only if the listener needs time to understand it" in SHOWRUNNER_SYSTEM
    assert "Good backbone usually introduces a necessary mechanism" in SHOWRUNNER_SYSTEM
    assert "A claim is weak backbone when it mainly lists surface details" in SHOWRUNNER_SYSTEM
    assert "Vivid detail is an entrance, not a destination." in SHOWRUNNER_SYSTEM
    assert "Dense mechanisms, crises, and consequential decisions need more oxygen" in SHOWRUNNER_SYSTEM
    assert "Never require stress-tests by default" in SHOWRUNNER_SYSTEM
    assert "proof-under-pressure case only when it beats weaker support/comparator/context" in SHOWRUNNER_SYSTEM
    assert "treat that as a sign of overload" in SHOWRUNNER_SYSTEM
    assert "one dominant explanatory spine" in SHOWRUNNER_SYSTEM
    assert "real-world test" in SHOWRUNNER_SYSTEM
    assert "Ignore research-tree provenance when it does not help the editorial decision." in SHOWRUNNER_SYSTEM
    assert "Comparative material should stay selective." in SHOWRUNNER_SYSTEM
    assert "total_claim_count" in SHOWRUNNER_SYSTEM
    assert "Use `total_claim_count` as an overload smell, not a quota" in SHOWRUNNER_SYSTEM
    assert "The last act should include takeaways and a clear \"why this matters\" payoff." in SHOWRUNNER_SYSTEM
    assert "The last act should contain only claims that directly sharpen the final takeaway or reframe the episode-level question." in SHOWRUNNER_SYSTEM
    assert "Do not use the last act to park leftover operational facts that could be removed without changing the final takeaway." in SHOWRUNNER_SYSTEM

    top_level_rules = [line for line in SHOWRUNNER_SYSTEM.splitlines() if line.startswith("-")]
    assert len(top_level_rules) <= 20
    assert len(SHOWRUNNER_SYSTEM.split()) < 1350


def test_showrunner_revision_prompt_uses_revision_contract() -> None:
    assert "revising an existing podcast act plan" in SHOWRUNNER_REVISION_SYSTEM
    assert "`current_plan`: the current act plan to revise" in SHOWRUNNER_REVISION_SYSTEM
    assert "`revision_request.selection_issues`" in SHOWRUNNER_REVISION_SYSTEM
    assert "revise `current_plan`, not regenerate from scratch" in SHOWRUNNER_REVISION_SYSTEM
    assert "Treat `revision_request.selection_issues` as the highest-priority editorial diagnosis" in SHOWRUNNER_REVISION_SYSTEM
    assert "Do not output a combined `claim_ids` list" in SHOWRUNNER_REVISION_SYSTEM
    assert "resolve tradeoffs across issues" in SHOWRUNNER_REVISION_SYSTEM
    assert "`minimal_backbone_claim_ids`" in SHOWRUNNER_REVISION_SYSTEM
    assert "`revision_mode`" in SHOWRUNNER_REVISION_SYSTEM
    assert "`act_rebuild` means rebuild the implicated act" in SHOWRUNNER_REVISION_SYSTEM
    assert "Preserve acts, timings, and claim placements that are not implicated by the issues." in SHOWRUNNER_REVISION_SYSTEM
    assert "If setup/hook backbone prematurely answers the episode question" in SHOWRUNNER_REVISION_SYSTEM
    assert "move them into the later explain/resolve act that owns the reveal" in SHOWRUNNER_REVISION_SYSTEM
    assert "If setup/hook is overfunded for anomaly/recognition work" in SHOWRUNNER_REVISION_SYSTEM
    assert "Do not convert sequencing issues into a fixed story template." in SHOWRUNNER_REVISION_SYSTEM
    assert "Choose the order that best serves the act's own `act_job`" in SHOWRUNNER_REVISION_SYSTEM
    assert "Before applying a requested claim swap, identify the listener-work gap it fixes." in SHOWRUNNER_REVISION_SYSTEM
    assert "If the issue is only claim preference, inventory expansion, or stronger-but-nonessential detail" in SHOWRUNNER_REVISION_SYSTEM
    assert "a requested causal/choice/rationale claim that fixes the gap belongs in the implicated act's backbone" in SHOWRUNNER_REVISION_SYSTEM
    assert "Do not swing in the next revision by demoting a causal/rationale claim merely because it answers well" in SHOWRUNNER_REVISION_SYSTEM
    assert "keep the backbone on visible anomaly/stakes" in SHOWRUNNER_REVISION_SYSTEM
    assert "Do not add a stress-test/case-study by default" in SHOWRUNNER_REVISION_SYSTEM


def test_showrunner_selection_audit_prompt_uses_scope_and_revision_mode_contract() -> None:
    from app.pipeline.phase5_planning.showrunner import SHOWRUNNER_SELECTION_AUDIT_SYSTEM

    assert '"scope": "episode|act|claim_role|claim"' in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert '"revision_mode": "local_edit|act_rebuild|episode_rebuild"' in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert '"diagnosis": "short explanation of the editorial problem"' in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert '"rationale": "why the plan passed or failed' in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "`minimal_backbone_claim_ids`" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Audit only material selection/design failures" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "claim is in the wrong airtime role" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Audit the same airtime-worthiness standard the showrunner used" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Mandatory first check" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "answer/control/issuance-condition/reserve/peg/mechanism claims kept out of backbone" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Causal-claim anti-pendulum" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "do not call a causal/rationale claim premature when it directly answers the current `opening_question`/`act_job`" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Would the act still work if a backbone claim were removed?" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "`estimated_duration_seconds` match the act's actual listener work" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "lightweight hook/setup/bridge being padded" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "setup/hook mainly poses an anomaly or recognition beat" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Does a setup/hook act prematurely put answer/control/issuance-condition/reserve/peg/mechanism claims in backbone" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "mini-solving the episode question before a later explain/resolve act can land the reveal" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "has the plan made decorative setup the destination?" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Do dense mechanisms, crises, and consequential decisions get enough oxygen" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Is the plan coverage-first" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Compare selected backbone claims against omitted claims as opportunity cost" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Do not require a stress-test/case-study beat for every topic" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "claims actually contain it" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "the plan lacks an equivalent test" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "If an act asks why/what allowed/why persisted but backbone only proves sequence or existence" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "avoid treating zero-direct-support claims as backbone" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Return compact JSON only" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "rationale <= 90 words" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Pass when the selected act spine performs the stated listener work" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "remaining alternatives are optional improvements" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Use `medium` or `high` only for blockers" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Use `low` for non-blocking improvements" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Each issue must first name the broken listener-work invariant" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Claim IDs are evidence for that diagnosis, not the diagnosis itself." in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Do not ask for claim swaps just because an omitted claim is interesting" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "only when the current backbone cannot perform the stated listener work without it" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Do not prescribe a universal story order." in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "diagnose the broken listener question, missing turn, or unsupported act job" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "do not fail a plan merely because it puts event before mechanism" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Always include `rationale`" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "name the strongest omitted opportunity-cost candidate" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Resolve tradeoffs inside the audit" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "Each selected claim object includes `role`" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "do not ask to remove a claim as redundant and then require it as missing support" in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "weak_claim_selected" not in SHOWRUNNER_SELECTION_AUDIT_SYSTEM
    assert "hook_overweights_decorative_detail" not in SHOWRUNNER_SELECTION_AUDIT_SYSTEM


def test_showrunner_generation_metadata_defaults_to_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_generation_provider",
        "anthropic",
        raising=False,
    )

    assert _showrunner_generation_metadata() == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "",
    }


def test_showrunner_generation_metadata_normalizes_unsupported_openai_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_generation_provider",
        "openai",
        raising=False,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_openai_model",
        "gpt-5.4",
        raising=False,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_openai_reasoning_effort",
        "minimal",
        raising=False,
    )

    assert _showrunner_generation_metadata()["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_showrunner_generation_can_route_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def _fake_openai_structured(**kwargs):
        calls.append(kwargs)
        return '{"acts":[]}'

    async def _fail_sonnet_structured(**_kwargs):
        raise AssertionError("showrunner should use OpenAI when configured")

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_generation_provider",
        "openai",
        raising=False,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_openai_model",
        "gpt-5.4",
        raising=False,
    )
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.showrunner_openai_reasoning_effort",
        "medium",
        raising=False,
    )
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner.call_openai_structured", _fake_openai_structured)
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner.call_sonnet_structured", _fail_sonnet_structured)

    raw = await _call_showrunner_generation_structured(
        system="system",
        prompt="prompt",
        temperature=0.5,
    )

    assert raw == '{"acts":[]}'
    assert calls == [
        {
            "system": "system",
            "prompt": "prompt",
            "model": "gpt-5.4",
            "schema": SHOWRUNNER_PLAN_SCHEMA,
            "schema_name": "showrunner_plan",
            "temperature": None,
            "reasoning_effort": "medium",
        }
    ]


def test_build_showrunner_prompt_payload_carries_output_language() -> None:
    payload = _build_showrunner_prompt_payload(
        duration_target_seconds=1800,
        output_language="ru",
        show_style="Conversational",
        episode_goal="Explain why Hong Kong money works this way.",
        root_question="Why does Hong Kong let private banks issue money?",
        claims=[
            {
                "id": "c1",
                "statement": "test",
                "branch_ids": ["b1"],
                "question_ids": ["b1.1"],
            }
        ],
        conflicts=[],
        disputed_claim_ids=[],
        duration_fill_layers=[],
    )

    assert payload["duration_target_seconds"] == 1800
    assert payload["output_language"] == "ru"
    assert payload["episode_goal"] == "Explain why Hong Kong money works this way."
    assert payload["root_question"] == "Why does Hong Kong let private banks issue money?"
    assert "rigor_policy" not in payload
    assert payload["total_claim_count"] == 1
    assert "branch_ids" not in payload["claims"][0]
    assert "question_ids" not in payload["claims"][0]
    assert payload["duration_fill_layers"] == []


def test_build_showrunner_revision_payload_includes_current_plan_and_issues() -> None:
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])

    payload = _build_showrunner_revision_payload(
        base_payload={
            "duration_target_seconds": 1800,
            "output_language": "ru",
            "claims": [{"id": "c1", "statement": "test"}],
        },
        current_plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "purpose": "Explain it.",
                    "internal_arc": "rule -> consequence",
                    "opening_question": "How?",
                    "ending_turn": "Clear enough.",
                    "estimated_duration_seconds": 300,
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        revision_request={
            "message": "Fix the plan.",
            "selection_issues": [{"scope": "claim", "revision_mode": "local_edit", "severity": "medium"}],
        },
        sid=sid,
    )

    assert "claim_ids" not in payload["current_plan"]["acts"][0]
    assert payload["current_plan"]["acts"][0]["backbone_claim_ids"] == ["c1"]
    assert payload["revision_request"]["message"] == "Fix the plan."


def test_shorten_plan_claim_ids_preserves_act_claim_roles() -> None:
    claim_uuid_one = "11111111-1111-1111-1111-111111111111"
    claim_uuid_two = "22222222-2222-2222-2222-222222222222"
    claim_uuid_three = "33333333-3333-3333-3333-333333333333"
    sid = ShortIdMap([claim_uuid_one, claim_uuid_two, claim_uuid_three])

    payload = _shorten_plan_claim_ids(
        {
            "acts": [
                {
                    "act_number": 1,
                    "claim_ids": [claim_uuid_one, claim_uuid_two, claim_uuid_three],
                    "backbone_claim_ids": [claim_uuid_one],
                    "support_claim_ids": [claim_uuid_two],
                    "callback_claim_ids": [claim_uuid_three],
                }
            ]
        },
        sid=sid,
    )

    assert "claim_ids" not in payload["acts"][0]
    assert payload["acts"][0]["backbone_claim_ids"] == ["c1"]
    assert payload["acts"][0]["support_claim_ids"] == ["c2"]
    assert payload["acts"][0]["callback_claim_ids"] == ["c3"]


def test_normalize_showrunner_selection_issues_keeps_structured_issue_contract() -> None:
    normalized = _normalize_showrunner_selection_issues(
        [
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "act_number": 2,
                "diagnosis": "Decorative detail is promoted to backbone.",
                "drop_or_demote_claim_ids": ["c40"],
                "support_or_callback_claim_ids": ["c3"],
                "fix_direction": "Drop the weaker detail.",
            },
            {
                "severity": "high",
                "scope": "act",
                "revision_mode": "act_rebuild",
                "minimal_backbone_claim_ids": ["c10", "c11"],
                "diagnosis": "The act cannot explain its own mechanism.",
                "fix_direction": "Preserve the smallest viable spine.",
            },
            {
                "severity": "high",
                "scope": "made_up",
                "revision_mode": "act_rebuild",
                "diagnosis": "Should be discarded.",
                "fix_direction": "Ignore me.",
            },
            {
                "severity": "high",
                "scope": "act",
                "revision_mode": "made_up",
                "diagnosis": "Should also be discarded.",
                "fix_direction": "Ignore me.",
            },
        ]
    )

    assert normalized == [
        {
            "severity": "medium",
            "scope": "claim_role",
            "revision_mode": "local_edit",
            "act_number": 2,
            "diagnosis": "Decorative detail is promoted to backbone.",
            "drop_or_demote_claim_ids": ["c40"],
            "support_or_callback_claim_ids": ["c3"],
            "fix_direction": "Drop the weaker detail.",
        },
        {
            "severity": "high",
            "scope": "act",
            "revision_mode": "act_rebuild",
            "diagnosis": "The act cannot explain its own mechanism.",
            "minimal_backbone_claim_ids": ["c10", "c11"],
            "fix_direction": "Preserve the smallest viable spine.",
        },
    ]


def test_showrunner_selection_blockers_only_returns_medium_or_high() -> None:
    blockers = _showrunner_selection_blockers(
        {
            "issues": [
                {"scope": "claim", "revision_mode": "local_edit", "severity": "low"},
                {"scope": "episode", "revision_mode": "episode_rebuild", "severity": "medium"},
                {"scope": "act", "revision_mode": "act_rebuild", "severity": "high"},
            ]
        }
    )

    assert blockers == [
        {"scope": "episode", "revision_mode": "episode_rebuild", "severity": "medium"},
        {"scope": "act", "revision_mode": "act_rebuild", "severity": "high"},
    ]


def test_validate_showrunner_selection_issue_ids_drops_invented_claim_ids() -> None:
    validated = _validate_showrunner_selection_issue_ids(
        [
            {
                "severity": "high",
                "scope": "claim",
                "revision_mode": "local_edit",
                "add_or_promote_claim_ids": ["c888"],
                "diagnosis": "Invented IDs.",
                "fix_direction": "Should not matter.",
            },
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "drop_or_demote_claim_ids": ["c1", "c999"],
                "add_or_promote_claim_ids": ["c2", "c888"],
                "diagnosis": "Valid selected claim has a weaker role.",
                "fix_direction": "Use valid IDs only.",
            },
        ],
        selected_ids={"c1"},
        omitted_ids={"c2"},
    )

    assert validated == [
        {
            "severity": "medium",
            "scope": "claim_role",
            "revision_mode": "local_edit",
            "diagnosis": "Valid selected claim has a weaker role.",
            "fix_direction": "Use valid IDs only.",
            "drop_or_demote_claim_ids": ["c1"],
            "add_or_promote_claim_ids": ["c2"],
        }
    ]


def test_validate_showrunner_selection_issue_ids_handles_scoped_structural_issues() -> None:
    validated = _validate_showrunner_selection_issue_ids(
        [
            {
                "severity": "high",
                "scope": "episode",
                "revision_mode": "episode_rebuild",
                "diagnosis": "Different listener jobs are compressed into one act.",
                "fix_direction": "Split the mechanism and crisis into separate acts.",
            },
            {
                "severity": "high",
                "scope": "act",
                "revision_mode": "act_rebuild",
                "minimal_backbone_claim_ids": ["c1", "c2", "c999"],
                "add_or_promote_claim_ids": ["c3", "c888"],
                "diagnosis": "The act lacks the minimal mechanism spine.",
                "fix_direction": "Build around the valid spine.",
            },
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "drop_or_demote_claim_ids": ["c999"],
                "diagnosis": "Only invented IDs.",
                "fix_direction": "Should be dropped.",
            },
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "drop_or_demote_claim_ids": ["c2", "c999"],
                "support_or_callback_claim_ids": ["c2", "c999"],
                "diagnosis": "The setup spends too much time on decorative detail.",
                "fix_direction": "Downgrade the vivid detail to support.",
            },
            {
                "severity": "medium",
                "scope": "claim",
                "revision_mode": "local_edit",
                "affected_claim_ids": ["c999"],
                "diagnosis": "No valid selected IDs.",
                "fix_direction": "Should be dropped.",
            },
        ],
        selected_ids={"c1", "c2"},
        omitted_ids={"c3"},
        all_ids={"c1", "c2", "c3"},
    )

    assert validated == [
        {
            "severity": "high",
            "scope": "episode",
            "revision_mode": "episode_rebuild",
            "diagnosis": "Different listener jobs are compressed into one act.",
            "fix_direction": "Split the mechanism and crisis into separate acts.",
        },
        {
            "severity": "high",
            "scope": "act",
            "revision_mode": "act_rebuild",
            "diagnosis": "The act lacks the minimal mechanism spine.",
            "fix_direction": "Build around the valid spine.",
            "minimal_backbone_claim_ids": ["c1", "c2"],
            "add_or_promote_claim_ids": ["c3"],
        },
        {
            "severity": "medium",
            "scope": "claim_role",
            "revision_mode": "local_edit",
            "diagnosis": "The setup spends too much time on decorative detail.",
            "fix_direction": "Downgrade the vivid detail to support.",
            "drop_or_demote_claim_ids": ["c2"],
            "support_or_callback_claim_ids": ["c2"],
        },
    ]


def test_validate_showrunner_selection_issue_ids_drops_contradictory_role_repairs() -> None:
    validated = _validate_showrunner_selection_issue_ids(
        [
            {
                "severity": "high",
                "scope": "act",
                "revision_mode": "act_rebuild",
                "act_number": 4,
                "diagnosis": "The issue asks for the same claim as required backbone and demotion.",
                "fix_direction": "This should not survive post-validation.",
                "minimal_backbone_claim_ids": ["c1", "c2"],
                "drop_or_demote_claim_ids": ["c1"],
                "support_or_callback_claim_ids": ["c1"],
                "add_or_promote_claim_ids": ["c3"],
            }
        ],
        selected_ids={"c1", "c2"},
        omitted_ids={"c3"},
        all_ids={"c1", "c2", "c3"},
        act_roles={
            4: {
                "backbone": {"c1", "c2"},
                "support": set(),
                "callback": set(),
                "selected": {"c1", "c2"},
            }
        },
    )

    assert validated == []


def test_validate_showrunner_selection_issue_ids_filters_noop_and_mismatched_act_roles() -> None:
    validated = _validate_showrunner_selection_issue_ids(
        [
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "act_number": 1,
                "diagnosis": "Callback demotion is a no-op.",
                "fix_direction": "Should drop.",
                "drop_or_demote_claim_ids": ["c3"],
            },
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "act_number": 1,
                "diagnosis": "Claim is selected in another act, not this one.",
                "fix_direction": "Should drop.",
                "drop_or_demote_claim_ids": ["c4"],
            },
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "act_number": 1,
                "diagnosis": "Already backbone.",
                "fix_direction": "Should drop.",
                "add_or_promote_claim_ids": ["c1"],
            },
            {
                "severity": "medium",
                "scope": "claim_role",
                "revision_mode": "local_edit",
                "act_number": 1,
                "diagnosis": "Backbone claim can be demoted.",
                "fix_direction": "Move to support.",
                "drop_or_demote_claim_ids": ["c1"],
                "support_or_callback_claim_ids": ["c1"],
            },
            {
                "severity": "medium",
                "scope": "claim",
                "revision_mode": "local_edit",
                "act_number": 1,
                "diagnosis": "Support claim can be promoted.",
                "fix_direction": "Promote to backbone.",
                "add_or_promote_claim_ids": ["c2"],
            },
        ],
        selected_ids={"c1", "c2", "c3", "c4"},
        omitted_ids={"c5"},
        all_ids={"c1", "c2", "c3", "c4", "c5"},
        act_roles={
            1: {
                "backbone": {"c1"},
                "support": {"c2"},
                "callback": {"c3"},
                "selected": {"c1", "c2", "c3"},
            },
            2: {
                "backbone": {"c4"},
                "support": set(),
                "callback": set(),
                "selected": {"c4"},
            },
        },
    )

    assert validated == [
        {
            "severity": "medium",
            "scope": "claim_role",
            "revision_mode": "local_edit",
            "act_number": 1,
            "diagnosis": "Backbone claim can be demoted.",
            "fix_direction": "Move to support.",
            "drop_or_demote_claim_ids": ["c1"],
            "support_or_callback_claim_ids": ["c1"],
        },
        {
            "severity": "medium",
            "scope": "claim",
            "revision_mode": "local_edit",
            "act_number": 1,
            "diagnosis": "Support claim can be promoted.",
            "fix_direction": "Promote to backbone.",
            "add_or_promote_claim_ids": ["c2"],
        },
    ]


def test_selection_audit_raw_signals_failure_detects_invalid_negative_audits() -> None:
    assert _selection_audit_raw_signals_failure('```json\n{"passed": false, "issues": []')
    assert _selection_audit_raw_signals_failure('{"issues":[{"severity":"high"}')
    assert _selection_audit_raw_signals_failure('{"issues":[{"severity": "medium"}')
    assert not _selection_audit_raw_signals_failure("not json")


def test_selection_audit_uses_short_id_namespace_for_normalized_plan() -> None:
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    normalized_plan = {
        "act_count": 1,
        "acts": [
            {
                "act_number": 1,
                "story_role": "explain",
                "title": "Act 1",
                "purpose": "Explain the mechanism.",
                "act_job": "explain the constraint",
                "internal_arc": "rule -> consequence",
                "opening_question": "How does it work?",
                "ending_turn": "The mechanism becomes concrete.",
                "estimated_duration_seconds": 300,
                "claim_ids": [claim_uuid],
                "backbone_claim_ids": [claim_uuid],
                "support_claim_ids": [],
                "callback_claim_ids": [],
                "why_this_is_enough": "One claim carries the mechanism.",
            }
        ],
    }
    prompt_claim_lookup = {
        "c1": {
            "id": "c1",
            "statement": "Hong Kong banks issue notes against USD backing.",
            "type": "FACT",
            "verification": "verified",
            "intents": ["explain"],
            "has_illustrative": False,
            "direct_support_count": 2,
            "source_diversity": 2,
        }
    }

    audit_plan = _shorten_plan_for_selection_audit(normalized_plan, sid=sid)

    assert _selected_claim_ids(audit_plan) == ["c1"]
    act_view = _compact_selection_act_view(audit_plan, prompt_claim_lookup)[0]
    assert act_view["act_job"] == "explain the constraint"
    assert act_view["backbone_claim_ids"] == ["c1"]
    assert act_view["support_claim_ids"] == []
    assert act_view["callback_claim_ids"] == []
    assert "claim_ids" not in act_view
    assert act_view["why_this_is_enough"] == "One claim carries the mechanism."
    assert act_view["claims"] == [
        {
            "id": "c1",
            "statement": "Hong Kong banks issue notes against USD backing.",
            "type": "FACT",
            "verification": "verified",
            "intents": ["explain"],
            "has_illustrative": False,
            "direct_support_count": 2,
            "source_diversity": 2,
            "role": "backbone",
        }
    ]


@pytest.mark.asyncio
async def test_selection_audit_failure_is_advisory_not_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bad_haiku(**_kwargs):
        return "not json"

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _bad_haiku,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                }
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert audit["passed"] is True
    assert audit["rationale"] == ""
    assert audit["issues"] == []
    assert audit["audit_failed"] is True
    assert audit["raw_signaled_failure"] is False
    assert audit["error_type"] == "ValueError"
    assert "No valid JSON" in audit["error"]
    assert audit["raw"] == "not json"
    assert audit["parsed"] is None


@pytest.mark.asyncio
async def test_plan_episode_returns_latest_candidate_after_selection_audit_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    generation_calls = []
    audit_calls = []
    debug_dumps = []

    async def _load_planning_context(**_kwargs):
        return {
            claim_uuid: {
                "id": claim_uuid,
                "type": "FACT",
                "verification": "verified",
                "statement": "Hong Kong banks issue notes against USD backing.",
                "supporting_evidence_count": 2,
                "illustrative_evidence_count": 0,
                "has_illustrative": False,
                "branch_ids": [],
                "question_ids": [],
                "intents": ["explain"],
                "direct_support_count": 2,
                "contextual_support_count": 0,
                "source_diversity": 2,
                "quality_hints": {
                    "has_primary_support": True,
                    "has_high_reliability_support": True,
                    "temporary_only_support": False,
                },
            }
        }, {}

    async def _valid_generation(**kwargs):
        generation_calls.append(kwargs)
        return json.dumps(
            {
                "act_count": 1,
                "acts": [
                    {
                        "act_number": 1,
                        "story_role": "explain",
                        "title": "The Monetary Constraint",
                        "purpose": "Explain why private notes can still be constrained.",
                        "act_job": "teach the reserve-backed issuance mechanism",
                        "internal_arc": "oddity -> constraint -> implication",
                        "opening_question": "Who actually constrains the notes?",
                        "ending_turn": "Private issuance becomes a rule-bound public monetary design.",
                        "estimated_duration_seconds": 1800,
                        "backbone_claim_ids": ["c1"],
                        "support_claim_ids": [],
                        "callback_claim_ids": [],
                        "claim_ids": ["c1"],
                        "conflict_brief_ids": [],
                        "content_quotas": {
                            "verified_facts": 1,
                            "attributed_opinions": 0,
                            "narrative_assets": 0,
                            "conflicts": 0,
                            "takeaways": 1,
                        },
                        "why_this_is_enough": "One well-supported mechanism claim carries the act.",
                    }
                ],
            }
        )

    async def _blocking_audit(**kwargs):
        audit_calls.append(kwargs)
        return {
            "passed": False,
            "rationale": "The act still hides the listener transformation.",
            "issues": [
                {
                    "severity": "high",
                    "scope": "act",
                    "act_number": 1,
                    "revision_mode": "act_rebuild",
                    "diagnosis": "The act does not make the editorial turn explicit enough.",
                    "fix_direction": "Make the act's listener transformation sharper.",
                }
            ],
        }

    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner.async_session_factory", lambda: _NoopAsyncSession())
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner.load_planning_context", _load_planning_context)
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner._call_showrunner_generation_structured", _valid_generation)
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner._run_showrunner_selection_audit", _blocking_audit)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner._showrunner_generation_metadata",
        lambda: {"provider": "anthropic", "model": "test", "reasoning_effort": ""},
    )
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.update_generation_progress",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("app.pipeline.phase5_planning.showrunner._dump_showrunner_debug", lambda **kwargs: debug_dumps.append(kwargs))

    result = await plan_episode(
        {
            "generation_run_id": "run-selection-audit-advisory",
            "duration_target": 1800,
            "language": "en",
            "user_prompt": "Explain Hong Kong private banknotes.",
            "claim_ids": [claim_uuid],
            "conflict_brief_ids": [],
        }
    )

    assert result["current_phase"] == "phase5_planning"
    assert len(generation_calls) == 3
    assert len(audit_calls) == 3
    assert debug_dumps[-1]["status"] == "ok_with_selection_audit_blockers"
    assert debug_dumps[-1]["normalized_plan"]["acts"][0]["claim_ids"] == [claim_uuid]
    assert debug_dumps[-1]["attempts"][-1]["accepted"] is False
    assert debug_dumps[-1]["attempts"][-1]["returned"] is True
    assert debug_dumps[-1]["attempts"][-1]["return_reason"] == "selection_audit_blockers_after_retries"


@pytest.mark.asyncio
async def test_selection_audit_parse_failure_blocks_when_raw_signals_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def _truncated_negative_audit(**kwargs):
        calls.append(kwargs)
        return '```json\n{"passed": false, "issues": [{"severity": "high", "scope": "act"'

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _truncated_negative_audit,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])

    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "duration_target_seconds": 1800,
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                }
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert calls[0]["max_tokens"] == SHOWRUNNER_SELECTION_AUDIT_MAX_TOKENS
    assert '"duration_target_seconds": 1800' in calls[0]["prompt"]
    assert audit["passed"] is False
    assert audit["audit_failed"] is True
    assert audit["raw_signaled_failure"] is True
    assert audit["issues"] == [
        {
            "severity": "high",
            "scope": "episode",
            "revision_mode": "episode_rebuild",
            "diagnosis": "Selection audit output was invalid or truncated while indicating the plan failed.",
            "fix_direction": (
                "Revise the candidate and rerun the audit with compact JSON; do not accept this plan "
                "as auditor-approved."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_selection_audit_drops_parseable_issues_with_invented_claim_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _invented_issue(**_kwargs):
        return (
            '{"passed": false, "rationale": "Invented omitted IDs do not survive validation.", "issues": ['
            '{"severity": "high", "scope": "claim", "revision_mode": "local_edit", '
            '"add_or_promote_claim_ids": ["c888"], '
            '"diagnosis": "Invented IDs.", "fix_direction": "Ignore."}'
            ']}'
        )

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _invented_issue,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
                {
                    "id": "c2",
                    "statement": "A stronger omitted claim.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["case_study"],
                    "has_illustrative": True,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert audit["passed"] is True
    assert audit["rationale"] == "Invented omitted IDs do not survive validation."
    assert audit["issues"] == []
    assert '"rationale": "Invented omitted IDs do not survive validation."' in audit["raw"]
    assert audit["parsed"]["passed"] is False


@pytest.mark.asyncio
async def test_selection_audit_validates_before_limiting_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _invalid_then_valid_issue(**_kwargs):
        invalid_issues = [
            {
                "severity": "high",
                "scope": "claim",
                "revision_mode": "local_edit",
                "add_or_promote_claim_ids": [f"c999{idx}"],
                "diagnosis": "Invented ID.",
                "fix_direction": "Ignore.",
            }
            for idx in range(4)
        ]
        valid_issue = {
            "severity": "high",
            "scope": "episode",
            "revision_mode": "episode_rebuild",
            "diagnosis": "The selected act set no longer answers the episode goal.",
            "fix_direction": "Reshape the backbone around the stated goal.",
        }
        return json.dumps(
            {
                "passed": False,
                "rationale": "The episode-level goal is not answered.",
                "issues": invalid_issues + [valid_issue],
            }
        )

    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.call_haiku_structured",
        _invalid_then_valid_issue,
    )
    claim_uuid = "11111111-1111-1111-1111-111111111111"
    sid = ShortIdMap([claim_uuid])
    audit = await _run_showrunner_selection_audit(
        prompt_payload={
            "episode_goal": "Explain why Hong Kong money works this way.",
            "root_question": "Why private banknotes?",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Hong Kong banks issue notes against USD backing.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["explain"],
                    "has_illustrative": False,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
                {
                    "id": "c2",
                    "statement": "A stronger omitted claim.",
                    "type": "FACT",
                    "verification": "verified",
                    "intents": ["case_study"],
                    "has_illustrative": True,
                    "direct_support_count": 2,
                    "source_diversity": 2,
                },
            ],
        },
        plan={
            "act_count": 1,
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "explain",
                    "title": "Act 1",
                    "claim_ids": [claim_uuid],
                }
            ],
        },
        sid=sid,
    )

    assert audit["passed"] is False
    assert audit["rationale"] == "The episode-level goal is not answered."
    assert audit["issues"] == [
        {
            "severity": "high",
            "scope": "episode",
            "revision_mode": "episode_rebuild",
            "diagnosis": "The selected act set no longer answers the episode goal.",
            "fix_direction": "Reshape the backbone around the stated goal.",
        }
    ]
    assert audit["parsed"]["rationale"] == "The episode-level goal is not answered."


def test_compute_max_claims_per_act_uses_ceiling() -> None:
    assert _compute_max_claims_per_act(52, 5) == 16
    assert _compute_max_claims_per_act(6, 3) == 6
    assert _compute_max_claims_per_act(0, 5) == 0


def test_rebalance_act_claim_distribution_leaves_overloaded_act_unchanged() -> None:
    claim_ids = [str(i) * 8 + "-" + str(i) * 4 + "-" + str(i) * 4 + "-" + str(i) * 4 + "-" + str(i) * 12 for i in range(1, 10)]
    plan = {
        "act_count": 3,
        "acts": [
            {
                "act_number": 1,
                "estimated_duration_seconds": 360,
                "claim_ids": claim_ids,
            },
            {
                "act_number": 2,
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
            {
                "act_number": 3,
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1" if idx < 8 else "b2"]}
        for idx, claim_id in enumerate(claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [9, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == claim_ids


def test_rebalance_act_claim_distribution_dedupes_cross_act_duplicates() -> None:
    claim_a = "11111111-1111-1111-1111-111111111111"
    claim_b = "22222222-2222-2222-2222-222222222222"
    plan = {
        "act_count": 2,
        "acts": [
            {
                "act_number": 1,
                "estimated_duration_seconds": 360,
                "claim_ids": [claim_a, claim_b],
            },
            {
                "act_number": 2,
                "estimated_duration_seconds": 360,
                "claim_ids": [claim_b],
            },
        ],
    }

    balanced = _rebalance_act_claim_distribution(
        plan,
        claim_lookup={
            claim_a: {"id": claim_a, "branch_ids": ["b1"]},
            claim_b: {"id": claim_b, "branch_ids": ["b2"]},
        },
    )

    assert balanced["acts"][0]["claim_ids"] == [claim_a, claim_b]
    assert balanced["acts"][1]["claim_ids"] == []


def test_rebalance_act_claim_distribution_rejects_role_payload_without_backbone() -> None:
    claim_id = "11111111-1111-1111-1111-111111111111"
    plan = {
        "act_count": 1,
        "acts": [
            {
                "act_number": 1,
                "estimated_duration_seconds": 300,
                "claim_ids": [claim_id],
                "backbone_claim_ids": [],
                "support_claim_ids": [claim_id],
                "callback_claim_ids": [],
            }
        ],
    }

    with pytest.raises(RuntimeError, match="selected claims but no backbone claims"):
        _rebalance_act_claim_distribution(
            plan,
            claim_lookup={claim_id: {"id": claim_id, "branch_ids": ["b1"]}},
        )


def test_rebalance_act_claim_distribution_never_moves_or_drops_overflow_claims() -> None:
    act_one_claim_ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666",
        "77777777-7777-7777-7777-777777777777",
        "88888888-8888-8888-8888-888888888888",
        "99999999-9999-9999-9999-999999999999",
    ]
    act_two_claim_ids = [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa5",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa6",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa7",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa8",
    ]
    plan = {
        "act_count": 4,
        "acts": [
            {
                "act_number": 1,
                "estimated_duration_seconds": 360,
                "claim_ids": act_one_claim_ids,
            },
            {
                "act_number": 2,
                "estimated_duration_seconds": 240,
                "claim_ids": act_two_claim_ids,
            },
            {
                "act_number": 3,
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
            {
                "act_number": 4,
                "estimated_duration_seconds": 240,
                "claim_ids": [],
            },
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1"]}
        for claim_id in (act_one_claim_ids + act_two_claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [9, 8, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == act_one_claim_ids
    assert balanced["acts"][1]["claim_ids"] == act_two_claim_ids


def test_rebalance_act_claim_distribution_uses_assigned_claim_count_for_ceiling() -> None:
    assigned_claim_ids = [
        f"{i:08d}-{i:04d}-{i:04d}-{i:04d}-{i:012d}"
        for i in range(1, 19)
    ]
    extra_claim_ids = [
        f"{i:08d}-{i:04d}-{i:04d}-{i:04d}-{i:012d}"
        for i in range(19, 97)
    ]
    plan = {
        "act_count": 5,
        "acts": [
            {
                "act_number": 1,
                "estimated_duration_seconds": 360,
                "claim_ids": assigned_claim_ids,
            },
            {"act_number": 2, "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 3, "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 4, "estimated_duration_seconds": 240, "claim_ids": []},
            {"act_number": 5, "estimated_duration_seconds": 240, "claim_ids": []},
        ],
    }
    claim_lookup = {
        claim_id: {"id": claim_id, "branch_ids": ["b1"]}
        for claim_id in (assigned_claim_ids + extra_claim_ids)
    }

    balanced = _rebalance_act_claim_distribution(plan, claim_lookup=claim_lookup)

    assert [len(act["claim_ids"]) for act in balanced["acts"]] == [18, 0, 0, 0, 0]
    assert balanced["acts"][0]["claim_ids"] == assigned_claim_ids
