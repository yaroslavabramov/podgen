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
        "source_order": ["S01"],
        "coverage_questions": [
            {
                "question_id": "q_main",
                "question": "Why did the arrangement emerge?",
                "kind": "cause",
                "importance": "critical",
                "rationale": "Directly serves the user request.",
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
        }
    )

    assert result["accepted"] is True
    assert saved
    assert workspace["coverage_questions"][0]["parent_question_id"] is None


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


def test_planner_prompt_preserves_intent_without_prejudging_sources() -> None:
    prompt = SOURCE_TRIAGE_PLANNER_PROMPT.casefold()

    assert "central question type" in prompt
    assert "requested explanatory task" in prompt
    assert "substantive question about the topic" in prompt
    assert "source ranking" in prompt and "not in coverage questions" in prompt
    assert "need for repair" in prompt and "previews" in prompt
    assert "distinct actors" in prompt
    assert "roles" in prompt
    assert "exceptions" in prompt


def test_legacy_repair_flag_without_saved_set_work_is_first_pass() -> None:
    payload = source_set_prompt_payload(_set_mode_workspace(), "set_1")

    assert payload["set"]["execution_mode"] == "first_pass"


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


def test_set_prompt_uses_derived_execution_modes_instead_of_raw_repair_flags() -> None:
    prompt = SOURCE_TRIAGE_SET_PROMPT.casefold()

    for execution_mode in ("first_pass", "resume", "targeted_repair"):
        assert execution_mode in prompt
    assert "repair=true" not in prompt
    assert "repair_targeted=true" not in prompt
