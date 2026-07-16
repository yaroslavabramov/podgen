from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.generation_v2 import runtime_checkpoint_snapshot
from app.generation_v2.runtime_checkpoint_snapshot import checkpoint_run_intent_payload
from app.generation_v2.runtime_checkpoint_paging import summarize_paginated_tool_traces
from app.generation_v2.runtime_checkpoint_restart import checkpoint_restart_instructions, checkpoint_restart_message
from app.generation_v2.runtime_checkpoint_context import (
    RESEARCH_FINALIZER_RESUME_CONTRACT,
    RESEARCH_JOB_RESUME_CONTRACT,
    RESEARCH_PLANNER_RESUME_CONTRACT,
    TRIAGE_FINALIZER_RESUME_CONTRACT,
    TRIAGE_PLANNER_RESUME_CONTRACT,
    TRIAGE_SET_RESUME_CONTRACT,
    build_checkpoint_resume_context,
)
from app.generation_v2.runtime_checkpoint_state import checkpoint_reason, validate_durable_checkpoint_state
from app.generation_v2.runtime_contracts import AgentCheckpointPolicy, default_checkpoint_policy
from app.generation_v2.schemas import ShowBibleContext
from app.generation_v2.prompting.source import (
    SOURCE_TRIAGE_FINALIZER_PROMPT,
    SOURCE_TRIAGE_PLANNER_PROMPT,
    SOURCE_TRIAGE_SET_PROMPT,
)
from app.generation_v2.source_triage_workspace_prompting import (
    checkpoint_workspace_payload,
    planner_resume_payload,
)


def _trace(tool_name: str, input_json: dict, output_json: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool_name, input_json=input_json, output_json=output_json)


def _resume_context(contract: str = TRIAGE_PLANNER_RESUME_CONTRACT):
    return build_checkpoint_resume_context(
        run_config={
            "language": "ru",
            "duration_target": 2700,
            "user_prompt": "История гонконгского доллара",
        },
        workflow_contract=contract,
    )


def _planning_workspace() -> dict:
    return {
        "status": "planning",
        "intent": {
            "language": "ru",
            "user_prompt": "История гонконгского доллара",
            "duration_seconds": 2700,
        },
        "source_order": ["S01", "S02", "S03"],
        "coverage_questions": [
            {
                "question_id": "q_origin",
                "question": "Как возникла частная эмиссия?",
                "kind": "origin",
                "importance": "critical",
                "rationale": "Нужна историческая основа.",
                "parent_question_id": None,
            },
            {
                "question_id": "q_mechanism",
                "question": "Как устроено обеспечение банкнот?",
                "kind": "mechanism",
                "importance": "critical",
                "rationale": "Нужно объяснить современный механизм.",
                "parent_question_id": None,
            },
        ],
        "source_sets": [
            {
                "set_id": "set_history",
                "label": "Истоки",
                "source_refs": ["S01"],
                "question_ids": ["q_origin"],
                "status": "planned",
            }
        ],
    }


def test_default_checkpoint_policy_uses_pressure_not_an_arbitrary_round_count() -> None:
    policy = default_checkpoint_policy("source_triage_planner_agent", phase="v2_source_triage")

    assert policy.enabled is True
    assert policy.max_chain_rounds is None
    assert checkpoint_reason(
        policy,
        chain_round=10_000,
        usage={"input_tokens": 12_852},
        chain_model_tool_output_chars=10_000,
    ) is None
    assert checkpoint_reason(
        AgentCheckpointPolicy(enabled=True, max_chain_rounds=8),
        chain_round=8,
        usage={"input_tokens": 100},
        chain_model_tool_output_chars=100,
    ) == "chain_rounds>=8"


def test_source_agent_prompts_label_embedded_context_as_an_initial_snapshot() -> None:
    assert "Initial durable triage workspace snapshot" in SOURCE_TRIAGE_PLANNER_PROMPT
    assert "Initial source-set and durable workspace snapshot" in SOURCE_TRIAGE_SET_PROMPT
    assert "Initial durable triage workspace snapshot" in SOURCE_TRIAGE_FINALIZER_PROMPT
    assert "Current triage workspace" not in SOURCE_TRIAGE_PLANNER_PROMPT


def test_planner_resume_payload_preserves_accepted_writes_and_computes_next_action() -> None:
    payload = planner_resume_payload(_planning_workspace())

    assert [item["question_id"] for item in payload["accepted_coverage_questions"]] == [
        "q_origin",
        "q_mechanism",
    ]
    assert payload["accepted_source_sets"][0]["source_refs"] == ["S01"]
    assert payload["missing_source_refs"] == ["S02", "S03"]
    assert payload["unassigned_question_ids"] == ["q_mechanism"]
    assert payload["next_action"]["tool_name"] == "upsert_triage_source_set"
    assert payload["next_action"]["source_refs_to_assign"] == ["S02", "S03"]


def test_checkpoint_workspace_carries_intent_and_exact_planner_progress() -> None:
    payload = checkpoint_workspace_payload(_planning_workspace())

    assert payload["intent"]["language"] == "ru"
    assert payload["intent"]["duration_seconds"] == 2700
    assert payload["intent"]["user_prompt_preview"] == "История гонконгского доллара"
    assert payload["planner_resume"]["accepted_coverage_question_count"] == 2
    assert payload["planner_resume"]["next_action"]["tool_name"] == "upsert_triage_source_set"


def test_generic_run_intent_survives_checkpoint_for_research_and_triage() -> None:
    payload = checkpoint_run_intent_payload(
        {
            "language": "ru",
            "user_prompt": "  История   гонконгского доллара  ",
            "duration_target": 2700,
        }
    )

    assert payload == {
        "language": "ru",
        "duration_seconds": 2700,
        "user_prompt_preview": "История гонконгского доллара",
        "user_prompt_chars": 28,
        "user_prompt_truncated": False,
    }


def test_durable_paging_progress_does_not_regress_when_a_later_chain_restarts_at_zero() -> None:
    traces = [
        _trace(
            "list_triage_source_sets",
            {"section": "coverage_questions", "cursor": "0", "limit": 3},
            {
                "accepted": True,
                "page": {"cursor": "0", "next_cursor": "3", "returned_count": 3, "total_count": 6},
                "items": [{}, {}, {}],
            },
        ),
        _trace(
            "list_triage_source_sets",
            {"section": "coverage_questions", "cursor": "3", "limit": 3},
            {
                "accepted": True,
                "page": {"cursor": "3", "next_cursor": "", "returned_count": 3, "total_count": 6},
                "items": [{}, {}, {}],
            },
        ),
        _trace(
            "list_triage_source_sets",
            {"section": "coverage_questions", "cursor": "0", "limit": 1},
            {
                "accepted": True,
                "page": {"cursor": "0", "next_cursor": "1", "returned_count": 1, "total_count": 7},
                "items": [{}],
            },
        ),
    ]

    progress = summarize_paginated_tool_traces(traces)

    assert len(progress) == 1
    assert progress[0]["seen_cursors"] == ["0", "3"]
    assert progress[0]["read_item_count"] == 6
    assert progress[0]["next_cursor"] == "6"
    assert progress[0]["all_pages_read"] is False


def test_targeted_capsule_read_is_complete_independently_of_corpus_size() -> None:
    progress = summarize_paginated_tool_traces(
        [
            _trace(
                "list_source_capsules",
                {"cursor": "0", "limit": 8, "source_refs": ["S08"]},
                {
                    "accepted": True,
                    "source_count": 36,
                    "returned_count": 1,
                    "next_cursor": "",
                    "capsules": [{}],
                },
            )
        ]
    )

    assert progress[0]["source_refs"] == ["S08"]
    assert progress[0]["total_count"] == 1
    assert progress[0]["all_pages_read"] is True
    assert progress[0]["next_cursor"] == ""


def test_checkpoint_message_tells_new_chain_what_not_to_repeat_and_where_to_continue() -> None:
    state = {
        "phase": "v2_source_triage",
        "agent_name": "source_triage_planner_agent",
        "source_triage_workspace": checkpoint_workspace_payload(_planning_workspace()),
        "durable_paging_progress": [
            {
                "tool_name": "list_triage_source_sets",
                "section": "coverage_questions",
                "next_cursor": "6",
                "all_pages_read": False,
            }
        ],
    }

    instructions = checkpoint_restart_instructions(state)
    message = checkpoint_restart_message(
        resume_context=_resume_context(),
        reason="input_tokens>=60000",
        previous_chain_id="chain-0",
        next_chain_id="chain-1",
        checkpoint_index=1,
        state=state,
        last_tool_results=[],
    )

    assert "do not switch languages" in instructions
    assert "next_cursor instead of cursor 0" in instructions
    assert "Never recreate, translate, or paraphrase" in instructions
    assert "q_origin" in message["content"]
    assert '"next_cursor": "6"' in message["content"]
    assert '"tool_name": "upsert_triage_source_set"' in message["content"]


def test_large_catalog_checkpoint_resumes_only_from_omitted_page() -> None:
    instructions = checkpoint_restart_instructions(
        {
            "phase": "v2_source_triage",
            "agent_name": "source_triage_planner_agent",
            "corpus": {
                "source_inventory_page": {
                    "total_count": 120,
                    "returned_count": 80,
                    "omitted_count": 40,
                    "next_cursor": "80",
                    "complete": False,
                }
            },
        }
    )

    assert "only the first durable catalog page" in instructions
    assert "cursor '80'" in instructions
    assert "complete catalog" not in instructions


def test_finalizer_checkpoint_requires_missing_or_invalidated_syntheses_before_finalize() -> None:
    instructions = checkpoint_restart_instructions(
        {
            "phase": "v2_source_triage",
            "agent_name": "source_triage_finalizer_agent",
        }
    )

    assert "coverage_question_packets" in instructions
    assert "upsert every missing or invalidated run-wide question synthesis" in instructions
    assert "backend derives lineage" in instructions
    assert instructions.index("question synthesis") < instructions.index("finalize_source_map")


def test_checkpoint_state_fails_closed_when_required_workspace_is_missing() -> None:
    with pytest.raises(RuntimeError, match="durable research workspace"):
        validate_durable_checkpoint_state(
            {"corpus": {}, "research_workspace_error": "database unavailable"},
            phase="v2_editorial_research",
            agent_name="research_planner_agent",
        )


def test_checkpoint_keeps_full_validated_user_intent_outside_mutable_state_compaction() -> None:
    user_intent = "Начало " + ("x" * 4_900) + " КОНЕЦ_ЗАМЫСЛА"
    context = build_checkpoint_resume_context(
        run_config={"language": "ru", "duration_target": 2700, "user_prompt": user_intent},
        workflow_contract=TRIAGE_SET_RESUME_CONTRACT,
    )

    content = checkpoint_restart_message(
        resume_context=context,
        reason="input_tokens>=60000",
        previous_chain_id="chain-0",
        next_chain_id="chain-1",
        checkpoint_index=1,
        state={"phase": "v2_source_triage", "agent_name": "source_triage_set_agent_set_1"},
        last_tool_results=[],
    )["content"]

    assert user_intent in content
    assert "КОНЕЦ_ЗАМЫСЛА" in content


def test_checkpoint_keeps_full_show_bible_outside_mutable_state_compaction() -> None:
    tail = "КОНЕЦ_SHOW_BIBLE"
    show_bible = ShowBibleContext(
        show_style=("style " * 180) + tail,
        hosts=[{"name": f"Host {index}", "bio": "bio " * 80} for index in range(10)],
        forbidden_phrases=[f"phrase-{index}" for index in range(25)],
    )
    context = build_checkpoint_resume_context(
        run_config={"language": "ru", "duration_target": 2700, "user_prompt": "Тема"},
        workflow_contract=RESEARCH_PLANNER_RESUME_CONTRACT,
        show_bible=show_bible,
    )

    assert context.task_context["show_bible"] == show_bible.model_dump(mode="json")
    assert "show_bible_preview" not in context.task_context
    content = checkpoint_restart_message(
        resume_context=context,
        reason="input_tokens>=60000",
        previous_chain_id="chain-0",
        next_chain_id="chain-1",
        checkpoint_index=1,
        state={"phase": "v2_editorial_research", "agent_name": "research_planner_agent"},
        last_tool_results=[],
    )["content"]

    assert tail in content
    assert "phrase-24" in content


def test_checkpoint_rejects_oversized_show_bible_instead_of_truncating_it() -> None:
    show_bible = ShowBibleContext(show_style="x" * 30_000)

    with pytest.raises(RuntimeError, match="task context exceeds"):
        build_checkpoint_resume_context(
            run_config={"language": "ru", "duration_target": 2700, "user_prompt": "Тема"},
            workflow_contract=RESEARCH_PLANNER_RESUME_CONTRACT,
            show_bible=show_bible,
        )


@pytest.mark.asyncio
async def test_restart_input_is_built_from_nonempty_durable_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _planning_workspace()

    async def load_workspace(_db: object, _run_id: object) -> dict:
        return workspace

    monkeypatch.setattr(
        "app.generation_v2.source_triage_workspace.load_source_triage_workspace",
        load_workspace,
    )
    state = {"phase": "v2_source_triage", "agent_name": "source_triage_planner_agent"}
    await runtime_checkpoint_snapshot._add_source_triage_workspace_state(  # type: ignore[attr-defined]
        object(),  # type: ignore[arg-type]
        state,
        generation_run_id=SimpleNamespace(),  # type: ignore[arg-type]
    )

    message = checkpoint_restart_message(
        resume_context=_resume_context(),
        reason="input_tokens>=60000",
        previous_chain_id="chain-0",
        next_chain_id="chain-1",
        checkpoint_index=1,
        state=state,
        last_tool_results=[],
    )["content"]

    assert '"language": "ru"' in message
    assert '"question_id": "q_origin"' in message
    assert '"question_id": "q_mechanism"' in message
    assert '"tool_name": "upsert_triage_source_set"' in message
    assert '"source_refs_to_assign": ["S02", "S03"]' in message


@pytest.mark.parametrize(
    ("phase", "agent_name", "contract", "state_key"),
    [
        ("v2_source_triage", "source_triage_planner_agent", TRIAGE_PLANNER_RESUME_CONTRACT, "source_triage_workspace"),
        ("v2_source_triage", "source_triage_set_agent_set_1", TRIAGE_SET_RESUME_CONTRACT, "source_triage_workspace"),
        ("v2_source_triage", "source_triage_finalizer_agent", TRIAGE_FINALIZER_RESUME_CONTRACT, "source_triage_workspace"),
        ("v2_editorial_research", "research_planner_agent", RESEARCH_PLANNER_RESUME_CONTRACT, "research_workspace"),
        ("v2_editorial_research", "research_job_agent_job_1", RESEARCH_JOB_RESUME_CONTRACT, "research_workspace"),
        ("v2_editorial_research", "research_finalizer_agent", RESEARCH_FINALIZER_RESUME_CONTRACT, "research_workspace"),
    ],
)
def test_checkpoint_restart_excludes_initial_rendered_state_for_every_stateful_role(
    phase: str,
    agent_name: str,
    contract: str,
    state_key: str,
) -> None:
    old_marker = "OLD_INITIAL_WORKSPACE_MARKER"
    fresh_marker = "FRESH_DB_STATE_MARKER"
    rendered_initial_prompt = f"Initial durable workspace snapshot: {old_marker}"
    state = {
        "phase": phase,
        "agent_name": agent_name,
        "corpus": {
            "source_inventory": [],
            "source_inventory_page": {
                "total_count": 0,
                "returned_count": 0,
                "next_cursor": "",
                "complete": True,
            },
        },
        state_key: {"status": fresh_marker},
    }

    content = checkpoint_restart_message(
        resume_context=_resume_context(contract),
        reason="input_tokens>=60000",
        previous_chain_id="chain-0",
        next_chain_id="chain-1",
        checkpoint_index=1,
        state=state,
        last_tool_results=[],
    )["content"]

    assert rendered_initial_prompt not in content
    assert old_marker not in content
    assert fresh_marker in content
    assert "task_brief_preview" not in content
