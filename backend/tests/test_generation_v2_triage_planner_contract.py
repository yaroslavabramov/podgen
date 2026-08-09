from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2 import source_triage_workspace as triage_workspace_module
from app.generation_v2.prompting.source import SOURCE_TRIAGE_PLANNER_PROMPT, SOURCE_TRIAGE_SET_PROMPT
from app.generation_v2.runtime_schema import strict_json_schema
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_tool_specs import source_triage_planner_tools
from app.generation_v2.source_triage_workspace_prompting import (
    checkpoint_workspace_payload,
    source_set_prompt_payload,
    unprocessed_source_sets,
)
from app.generation_v2.source_triage_workspace_tools import build_workspace_tools


async def _noop_handler(_args: dict) -> dict:
    return {"accepted": True}


def _planner_specs():
    return {
        tool.name: tool
        for tool in source_triage_planner_tools(
            _noop_handler,
            _noop_handler,
            _noop_handler,
            _noop_handler,
            _noop_handler,
        )
    }


def _json_schema_types(schema: dict) -> set[str]:
    direct_type = schema.get("type")
    if isinstance(direct_type, str):
        return {direct_type}
    if isinstance(direct_type, list):
        return {str(item) for item in direct_type}
    return {
        str(option.get("type"))
        for option in schema.get("anyOf") or []
        if isinstance(option, dict) and option.get("type")
    }


def _set_mode_workspace() -> dict:
    return {
        "intent": {"user_prompt": "Почему возникла система?", "language": "ru"},
        "source_order": ["S01"],
        "coverage_questions": [
            {
                "question_id": "q_main",
                "question": "Why did the arrangement emerge?",
                "kind": "cause",
                "importance": "critical",
                "rationale": "Directly serves the user request.",
                "answer_requirements": [
                    {
                        "requirement_id": "q_main:mechanism",
                        "description": "Explain the connecting mechanism.",
                        "rationale": "A cause is incomplete without its mechanism.",
                    }
                ],
            }
        ],
        "source_sets": [
            {
                "set_id": "set_1",
                "label": "Origins",
                "purpose": "Orient the causal question.",
                "source_refs": ["S01"],
                "question_ids": ["q_main"],
                "status": "needs_repair",
                "repair": True,
                "repair_targeted": False,
                "observations": [],
                "written_actions": [],
                "evidence_ids": [],
            }
        ],
        "source_assessments": [],
        "coverage_outcomes": [],
        "source_capsules": {},
    }


def _nested_dict_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _nested_dict_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _nested_dict_keys(item)}
    return set()


def test_interrupted_in_progress_set_is_selected_for_resume() -> None:
    workspace = _set_mode_workspace()
    workspace["source_sets"][0]["status"] = "in_progress"
    workspace["source_assessments"] = [{"set_id": "set_1", "source_ref": "S01"}]

    pending = unprocessed_source_sets(workspace)

    assert [item["set_id"] for item in pending] == ["set_1"]
    assert source_set_prompt_payload(workspace, "set_1")["set"]["execution_mode"] == "resume"


def test_planner_question_parent_schema_accepts_null_and_empty_string() -> None:
    parameters = strict_json_schema(_planner_specs()["upsert_coverage_question"].parameters)
    parent_schema = parameters["properties"]["parent_question_id"]

    assert _json_schema_types(parent_schema) == {"null", "string"}
    assert parent_schema.get("minLength", 0) == 0
    assert "parent_question_id" in parameters["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_value", [None, ""])
async def test_planner_accepts_root_question_without_fake_parent(
    monkeypatch: pytest.MonkeyPatch,
    parent_value: str | None,
) -> None:
    workspace = {"source_order": ["S01"], "coverage_questions": [], "source_sets": []}
    saved: list[dict] = []

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        saved.append(workspace)
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=None,
        mode="planner",
    )
    upsert_question = next(tool for tool in tools if tool.name == "upsert_coverage_question")

    result = await upsert_question.handler(
        {
            "question": "Why did the arrangement emerge?",
            "kind": "cause",
            "importance": "critical",
            "rationale": "This directly preserves the user's explanatory intent.",
            "parent_question_id": parent_value,
            "answer_requirements": [
                {
                    "description": "Identify the actors and connecting mechanism.",
                    "rationale": "These are required for a complete causal answer.",
                }
            ],
        }
    )

    assert result["accepted"] is True
    assert saved
    assert workspace["coverage_questions"][0]["parent_question_id"] is None
    assert workspace["coverage_questions"][0]["answer_requirements"][0]["description"] == (
        "Identify the actors and connecting mechanism."
    )


@pytest.mark.asyncio
async def test_planner_finalization_ignores_legacy_requirement_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {
        "source_order": ["S01"],
        "coverage_questions": [
            {
                "question_id": "q_main",
                "question": "Why did it happen?",
                "kind": "cause",
                "importance": "critical",
                "parent_question_id": None,
                "answer_requirements": [
                    {
                        "requirement_id": "q_main:new",
                        "description": "Explain the current required part.",
                        "rationale": "",
                    }
                ],
            }
        ],
        "source_sets": [
            {
                "set_id": "set_1",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": ["q_main"],
                "planned_question_ids": ["q_main"],
                "question_assignments": [
                    {"question_id": "q_main", "requirement_ids": ["q_main:removed"]}
                ],
            }
        ],
    }

    async def _load(_db, _run_id):
        return workspace

    saved: dict[str, object] = {}

    async def _save(_db, **kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=None,
        mode="planner",
    )
    finalize = next(tool for tool in tools if tool.name == "finalize_triage_workspace_plan")

    result = await finalize.handler(
        {"summary": "Plan complete.", "central_question_id": "q_main"}
    )

    assert result["accepted"] is True
    assert saved["workspace"]["status"] == "planned"  # type: ignore[index]


@pytest.mark.asyncio
async def test_planner_finalization_rejects_persisted_duplicate_question_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment = {"question_id": "q_main", "requirement_ids": ["q_main:mechanism"]}
    workspace = {
        "source_order": ["S01"],
        "coverage_questions": [
            {
                "question_id": "q_main",
                "question": "Why?",
                "importance": "critical",
                "parent_question_id": None,
                "answer_requirements": [
                    {
                        "requirement_id": "q_main:mechanism",
                        "description": "Explain the mechanism.",
                    }
                ],
            }
        ],
        "source_sets": [
            {
                "set_id": "set_1",
                "set_kind": "coverage",
                "source_refs": ["S01"],
                "question_ids": ["q_main"],
                "planned_question_ids": ["q_main"],
                "question_assignments": [assignment, dict(assignment)],
            }
        ],
    }

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, **_kwargs):
        raise AssertionError("Invalid plan must not be finalized")

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="planner",
    )
    finalize = next(tool for tool in tools if tool.name == "finalize_triage_workspace_plan")

    result = await finalize.handler(
        {"summary": "Plan complete.", "central_question_id": "q_main"}
    )

    assert result["accepted"] is False
    assert result["duplicate_question_assignments"] == [
        {"set_id": "set_1", "question_id": "q_main"}
    ]


@pytest.mark.asyncio
async def test_planner_rejects_unknown_question_parent_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {"source_order": ["S01"], "coverage_questions": [], "source_sets": []}
    save_calls = 0

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        nonlocal save_calls
        save_calls += 1
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=None,
        mode="planner",
    )
    upsert_question = next(tool for tool in tools if tool.name == "upsert_coverage_question")

    result = await upsert_question.handler(
        {
            "question": "What changed?",
            "kind": "change",
            "importance": "supporting",
            "rationale": "Orient later research.",
            "parent_question_id": "root/topic/invented",
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "parent_question_id must reference an existing question"
    assert workspace["coverage_questions"] == []
    assert save_calls == 0


@pytest.mark.asyncio
async def test_planner_rejects_self_parent_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {"source_order": ["S01"], "coverage_questions": [], "source_sets": []}
    save_calls = 0

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        nonlocal save_calls
        save_calls += 1
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=None,
        mode="planner",
    )
    upsert_question = next(tool for tool in tools if tool.name == "upsert_coverage_question")

    result = await upsert_question.handler(
        {
            "question_id": "q_self",
            "question": "What changed?",
            "kind": "change",
            "importance": "supporting",
            "rationale": "Orient later research.",
            "parent_question_id": "q_self",
        }
    )

    assert result["accepted"] is False
    assert "parent_question_id" in result["error"]
    assert workspace["coverage_questions"] == []
    assert save_calls == 0


def test_planner_source_set_schema_does_not_expose_repair_flag() -> None:
    assert "repair" not in _planner_specs()["upsert_triage_source_set"].parameters["properties"]


@pytest.mark.asyncio
async def test_planner_handler_rejects_repair_source_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    workspace = {
        "source_order": ["S01"],
        "coverage_questions": [{"question_id": "q_main", "question": "Why?"}],
        "source_sets": [],
    }
    save_calls = 0

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        nonlocal save_calls
        save_calls += 1
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=None,
        mode="planner",
    )
    upsert_set = next(tool for tool in tools if tool.name == "upsert_triage_source_set")

    result = await upsert_set.handler(
        {
            "label": "Ambiguous previews",
            "purpose": "Read the sources before judging them.",
            "source_refs": ["S01"],
            "rationale": "A compact preview is not a quality verdict.",
            "question_ids": ["q_main"],
            "priority": "normal",
            "repair": True,
        }
    )

    assert result["accepted"] is False
    assert "repair" in result["error"].casefold()
    assert workspace["source_sets"] == []
    assert save_calls == 0


@pytest.mark.asyncio
async def test_planner_rejects_duplicate_question_assignments_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = {
        "source_order": ["S01"],
        "coverage_questions": [
            {
                "question_id": "q_main",
                "question": "Why?",
                "answer_requirements": [
                    {"requirement_id": "q_main:cause", "description": "Establish the cause."},
                    {
                        "requirement_id": "q_main:mechanism",
                        "description": "Explain the mechanism.",
                    },
                ],
            }
        ],
        "source_sets": [],
    }
    save_calls = 0

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, **_kwargs):
        nonlocal save_calls
        save_calls += 1

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        mode="planner",
    )
    upsert_set = next(tool for tool in tools if tool.name == "upsert_triage_source_set")

    result = await upsert_set.handler(
        {
            "label": "Reading queue",
            "purpose": "Read the assigned source.",
            "source_refs": ["S01"],
            "rationale": "One source needs one owner.",
            "priority": "normal",
            "question_assignments": [
                {"question_id": "q_main", "requirement_ids": ["q_main:cause"]},
                {"question_id": "q_main", "requirement_ids": ["q_main:mechanism"]},
            ],
        }
    )

    assert result["accepted"] is False
    assert result["question_id"] == "q_main"
    assert "exactly one" in result["error"]
    assert workspace["source_sets"] == []
    assert save_calls == 0


def test_planner_prompt_preserves_intent_without_prejudging_sources() -> None:
    prompt = " ".join(SOURCE_TRIAGE_PLANNER_PROMPT.split()).casefold()

    assert "full actors, periods, comparisons, and requested relations" in prompt
    assert "semantic requirements of a complete answer" in prompt
    assert "no quota or fixed ontology" in prompt
    assert "responsibility for reading, never a thematic boundary" in prompt
    assert "neutral initial-reading set" in prompt
    assert "do not infer source quality or content from a weak preview" in prompt
    assert "do not answer the questions" in prompt


def test_legacy_repair_flag_without_saved_set_work_is_first_pass() -> None:
    payload = source_set_prompt_payload(_set_mode_workspace(), "set_1")

    assert payload["set"]["execution_mode"] == "first_pass"
    assert "output_language" not in payload


def test_set_payload_keeps_only_compact_index_for_other_questions() -> None:
    workspace = _set_mode_workspace()
    workspace["coverage_questions"].append(
        {
            "question_id": "q_unrelated",
            "question": "UNRELATED QUESTION TEXT",
            "kind": "context",
            "importance": "exploratory",
            "rationale": "UNRELATED RATIONALE",
            "answer_requirements": [
                {
                    "requirement_id": "q_unrelated:context",
                    "description": "UNRELATED REQUIREMENT TEXT",
                    "rationale": "UNRELATED REQUIREMENT RATIONALE",
                }
            ],
        }
    )
    workspace["source_sets"].append(
        {
            "set_id": "set_unrelated",
            "label": "UNRELATED SET LABEL",
            "purpose": "UNRELATED SET PURPOSE",
            "source_refs": [],
            "question_ids": ["q_unrelated"],
            "status": "planned",
        }
    )

    payload = source_set_prompt_payload(workspace, "set_1")
    encoded = str(payload)

    assert "UNRELATED QUESTION TEXT" in encoded
    assert "UNRELATED RATIONALE" not in encoded
    assert "UNRELATED SET LABEL" not in encoded
    assert "UNRELATED SET PURPOSE" not in encoded
    assert "source_sets" not in payload["workspace_progress"]
    assert "coverage_questions" not in payload["workspace_progress"]
    assert payload["question_count"] == 2
    assert payload["set"]["assigned_question_ids"] == ["q_main"]
    assert [item["question_id"] for item in payload["question_index"]] == ["q_main", "q_unrelated"]
    unrelated = payload["question_index"][1]
    assert unrelated["question"] == "UNRELATED QUESTION TEXT"
    assert unrelated["set_role"] == "available"
    assert [item["requirement_id"] for item in unrelated["answer_requirements"]] == [
        "q_unrelated:context"
    ]


def test_set_payload_exposes_backend_derived_pending_work() -> None:
    workspace = _set_mode_workspace()
    workspace["source_assessments"] = [
        {
            "set_id": "set_1",
            "source_ref": "S01",
            "processing_status": "needs_repair",
        }
    ]
    workspace["coverage_outcomes"] = [
        {"set_id": "set_1", "question_id": "q_main", "status": "needs_repair"}
    ]

    source_set = source_set_prompt_payload(workspace, "set_1")["set"]

    assert source_set["pending_source_refs"] == ["S01"]
    assert source_set["assigned_question_ids"] == ["q_main"]
    assert "required_question_ids" not in source_set
    assert "missing_outcome_question_ids" not in source_set


def test_other_question_index_has_no_fixed_item_quota() -> None:
    workspace = _set_mode_workspace()
    workspace["coverage_questions"].extend(
        {
            "question_id": f"q_other_{index}",
            "question": f"Other question {index} " + "x" * 260,
            "kind": "context",
            "importance": "exploratory",
            "rationale": "Full rationale stays out of the compact index.",
            "answer_requirements": [
                {
                    "requirement_id": f"q_other_{index}:context",
                    "description": f"Resolve context part {index}.",
                    "rationale": "Needed for the complete answer.",
                }
            ],
        }
        for index in range(100)
    )

    payload = source_set_prompt_payload(workspace, "set_1")

    assert len(payload["question_index"]) == 101
    assert payload["question_count"] == 101
    assert payload["question_index"][-1]["question_id"] == "q_other_99"
    assert "other_coverage_question_index" not in payload
    assert "other_coverage_question_omitted_count" not in payload


def test_assigned_questions_have_no_fixed_three_item_quota() -> None:
    workspace = _set_mode_workspace()
    workspace["coverage_questions"] = [
        {
            "question_id": f"q_{index}",
            "question": f"Assigned question {index}",
            "kind": "context",
            "importance": "supporting",
            "rationale": "r" * 800,
            "answer_requirements": [
                {
                    "requirement_id": f"q_{index}:context",
                    "description": f"Resolve assigned part {index}.",
                    "rationale": "Required for the assigned question.",
                }
            ],
        }
        for index in range(20)
    ]
    workspace["source_sets"][0]["question_ids"] = [
        item["question_id"] for item in workspace["coverage_questions"]
    ]

    source_set = source_set_prompt_payload(workspace, "set_1")["set"]

    assert len(source_set["assigned_question_ids"]) == 20
    assert source_set["assigned_question_count"] == 20


def test_assigned_questions_include_only_assigned_requirement_details() -> None:
    workspace = _set_mode_workspace()
    workspace["coverage_questions"][0]["answer_requirements"].append(
        {
            "requirement_id": "q_main:unassigned",
            "description": "This belongs to another set.",
            "rationale": "It should not be repeated in this set payload.",
        }
    )
    workspace["source_sets"][0]["question_assignments"] = [
        {"question_id": "q_main", "requirement_ids": ["q_main:mechanism"]}
    ]

    payload = source_set_prompt_payload(workspace, "set_1")
    assigned = payload["question_index"][0]

    assert [item["requirement_id"] for item in assigned["answer_requirements"]] == [
        "q_main:mechanism",
        "q_main:unassigned",
    ]
    assert "This belongs to another set." in str(payload["question_index"])


def test_set_payload_does_not_ship_preview_accounting_noise_or_duplicate_question_blocks() -> None:
    payload = source_set_prompt_payload(_set_mode_workspace(), "set_1")
    payload_keys = _nested_dict_keys(payload)

    assert not any(key.endswith("_chars") and not key.endswith("_omitted_chars") for key in payload_keys)
    assert not any(key.endswith("_truncated") for key in payload_keys)
    assert "coverage_questions" not in payload["set"]
    assert "question_assignments" not in payload["set"]
    assert "all_coverage_question_index" not in payload
    assert "assigned_questions" not in payload["set"]
    assert "other_coverage_question_index" not in payload
    assert "question_index" in payload


@pytest.mark.parametrize("saved_work", ["assessment", "outcome", "observation"])
def test_any_saved_set_work_derives_resume_mode(saved_work: str) -> None:
    workspace = _set_mode_workspace()
    if saved_work == "assessment":
        workspace["source_assessments"] = [{"set_id": "set_1", "source_ref": "S01"}]
    elif saved_work == "outcome":
        workspace["coverage_outcomes"] = [{"set_id": "set_1", "question_id": "q_main"}]
    else:
        workspace["source_sets"][0]["observations"] = [
            {"source_refs": ["S01"], "note": "Partial orientation was persisted."}
        ]

    payload = source_set_prompt_payload(workspace, "set_1")

    assert payload["set"]["execution_mode"] == "resume"


def test_targeted_repair_derives_targeted_repair_mode() -> None:
    workspace = _set_mode_workspace()
    workspace["source_sets"][0].update(
        {
            "repair_targeted": True,
            "repair_source_refs": ["S01"],
            "repair_question_ids": ["q_main"],
            "repair_targets": [
                {
                    "code": "missing_assessment",
                    "message": "Persist the missing assessment.",
                    "source_refs": ["S01"],
                    "question_ids": [],
                }
            ],
        }
    )

    payload = source_set_prompt_payload(workspace, "set_1")

    assert payload["set"]["execution_mode"] == "targeted_repair"


@pytest.mark.parametrize(
    ("targeted", "saved_work"),
    [(False, False), (False, True), (True, False)],
)
def test_raw_repair_flags_never_enter_set_agent_model_payload(targeted: bool, saved_work: bool) -> None:
    workspace = _set_mode_workspace()
    workspace["source_sets"][0]["repair_targeted"] = targeted
    if saved_work:
        workspace["source_sets"][0]["observations"] = [
            {"source_refs": ["S01"], "note": "Partial orientation was persisted."}
        ]

    payload = source_set_prompt_payload(workspace, "set_1")
    payload_keys = _nested_dict_keys(payload)

    assert "repair" not in payload_keys
    assert "repair_targeted" not in payload_keys


def test_raw_repair_flags_never_enter_set_agent_checkpoint_payload() -> None:
    workspace = _set_mode_workspace()
    workspace["current_set"] = {"set_id": "set_1"}
    workspace["source_sets"][0]["repair_targeted"] = True

    payload = checkpoint_workspace_payload(workspace)

    assert payload["current_set"]["execution_mode"] == "targeted_repair"
    assert "repair" not in _nested_dict_keys(payload)
    assert "repair_targeted" not in _nested_dict_keys(payload)


def test_set_prompt_uses_one_continuous_contract_without_mode_branches() -> None:
    prompt = SOURCE_TRIAGE_SET_PROMPT.casefold()

    for execution_mode in ("first_pass", "resume", "targeted_repair"):
        assert execution_mode not in prompt
    assert "one continuous agent" in prompt
    assert "read_triage_set_state" in prompt
    assert "durable source memory" in prompt
    assert "repair=true" not in prompt
    assert "repair_targeted=true" not in prompt


def test_set_prompt_requires_deep_reading_with_semantic_stopping_criteria() -> None:
    prompt = " ".join(SOURCE_TRIAGE_SET_PROMPT.split())

    assert "read_source returns the actual text as lossless passages" in prompt
    assert "search_source is navigation only, not proof" in prompt
    assert "name unread regions" in prompt
    assert "why further reading is unlikely to change source findings" in prompt
    assert "Before switching sources, save either a reading note or the completed source analysis" in prompt
    assert "Findings are atomic" in prompt
    assert "actor, period, geography, qualifiers" in prompt
    assert "passage_refs" in prompt
    assert "Finish only when unfinished_work is empty" in prompt
    assert "There are no quotas for reads, findings, evidence, or hints" in prompt


def test_set_prompt_does_not_treat_navigation_or_read_counts_as_semantic_evidence() -> None:
    prompt = " ".join(SOURCE_TRIAGE_SET_PROMPT.split())

    assert "search_source is navigation only, not proof" in prompt
    assert "Do not stop at the first match" in prompt
    assert "Zero findings is valid only after real reading with a concrete reason" in prompt
    assert "do not open, search, or read a different unresolved source" not in prompt.casefold()
    assert "minimum number" not in prompt.casefold()
    assert "maximum number" not in prompt.casefold()
