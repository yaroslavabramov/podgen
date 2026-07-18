from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_triage_canonical import (
    coverage_synthesis_input_hash,
    materialize_source_map_v2,
)
from app.generation_v2.source_triage_workspace_tool_handlers import (
    SourceTriageWorkspaceToolHandlers,
)
from app.generation_v2.source_triage_workspace_tool_specs import (
    source_triage_finalizer_tools,
    source_triage_set_tools,
)
from app.models.generation_v2 import EvidenceStrength


class _EvidenceToolset:
    def __init__(self, evidence: dict[str, SimpleNamespace], refs: dict[str, str]) -> None:
        self.evidence = evidence
        self.refs = refs
        self.selection_calls: list[dict] = []

    async def _evidence_in_run(self, evidence_id: str):
        return self.evidence.get(evidence_id)

    async def _source_ref_for_id(self, source_id) -> str:
        return self.refs.get(str(source_id), "S??")

    async def _select_evidence_ids_for_write(self, evidence_ids: list[str], **kwargs):
        self.selection_calls.append({"evidence_ids": list(evidence_ids), **kwargs})
        return []


def _card(source_id: str, title: str) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[str(uuid.uuid4())],
        title=title,
        source_type="web",
        summary=f"{title} summary.",
    )


def _evidence(evidence_id: str, source_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID(evidence_id),
        source_id=uuid.UUID(source_id),
        source_version_id=uuid.uuid4(),
        snippet_text="Grounded source text establishing the bounded comparison.",
        retrieval_channel="local_chunk_search",
        evidence_strength=EvidenceStrength.EXACT,
        metadata_={"usage_status": "observed"},
        purpose="source_read",
    )


def _workspace(source_ids: list[str], evidence_ids: list[str]) -> dict:
    source_a, source_b, source_c = source_ids
    evidence_a, evidence_b, _evidence_c = evidence_ids
    return {
        "schema_version": "source_triage_workspace.v3",
        "source_order": ["S01", "S02", "S03"],
        "source_sets": [
            {
                "set_id": "set-a",
                "source_refs": ["S01"],
                "question_ids": ["q1"],
                "evidence_ids": [evidence_a, evidence_b],
            },
            {"set_id": "set-b", "source_refs": ["S02"], "question_ids": ["q1"]},
            {"set_id": "set-c", "source_refs": ["S03"], "question_ids": []},
        ],
        "coverage_questions": [
            {
                "question_id": "q1",
                "question": "What changed?",
                "answer_requirements": [],
            }
        ],
        "source_assessments": [],
        "source_findings": [
            {
                "finding_id": "finding:S01:change",
                "set_id": "set-a",
                "source_ref": "S01",
                "source_id": source_a,
                "source_version_ids": [],
                "assessment_id": f"assessment:{source_a}",
                "statement": "The first source describes a bounded change.",
                "evidence_ids": [evidence_a],
                "scope": {"actor": "Actor", "period": "Period", "geography": "", "qualifiers": []},
                "limitations": [],
                "question_links": [
                    {"question_id": "q1", "requirement_ids": [], "rationale": "Direct support."}
                ],
                "source_relations": [
                    {
                        "related_source_ref": "S02",
                        "related_source_id": source_b,
                        "relationship": "duplicate",
                        "rationale": "Initial comparison.",
                        "evidence_ids": [evidence_a, evidence_b],
                    }
                ],
                "semantic_revision": 4,
            }
        ],
        "coverage_outcomes": [
            {
                "outcome_id": "outcome:q1:set-a",
                "set_id": "set-a",
                "question_id": "q1",
                "status": "covered",
                "finding_ids": ["finding:S01:change"],
                "requirement_results": [],
                "is_stale": False,
            }
        ],
        "coverage_syntheses": [],
        "triage_semantic_revision": 4,
        "source_ids": [source_a, source_b, source_c],
    }


def _handler(
    workspace: dict,
    source_ids: list[str],
    evidence_ids: list[str],
) -> tuple[SourceTriageWorkspaceToolHandlers, _EvidenceToolset]:
    refs = {source_id: f"S{index:02d}" for index, source_id in enumerate(source_ids, 1)}
    evidence = {
        evidence_id: _evidence(evidence_id, source_id)
        for evidence_id, source_id in zip(evidence_ids, source_ids, strict=True)
    }
    evidence_toolset = _EvidenceToolset(evidence, refs)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[
                _card(source_id, f"Source {index}")
                for index, source_id in enumerate(source_ids, 1)
            ]
        ),
        evidence_toolset=evidence_toolset,
    )
    return handler, evidence_toolset


def _relation(related_source_ref: str, evidence_ids: list[str]) -> dict:
    return {
        "related_source_ref": related_source_ref,
        "relationship": "updates",
        "rationale": "The first source explicitly updates the related source.",
        "evidence_ids": evidence_ids,
    }


@pytest.fixture
def relation_state(monkeypatch: pytest.MonkeyPatch) -> dict:
    source_ids = [str(uuid.uuid4()) for _ in range(3)]
    evidence_ids = [str(uuid.uuid4()) for _ in range(3)]
    workspace = _workspace(source_ids, evidence_ids)
    state = {"workspace": workspace, "save_count": 0}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        state["save_count"] += 1
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    state["handler"], state["evidence_toolset"] = _handler(
        workspace, source_ids, evidence_ids
    )
    state["source_ids"] = source_ids
    state["evidence_ids"] = evidence_ids
    return state


@pytest.mark.asyncio
async def test_finalizer_replaces_cross_set_relation_without_staling_coverage(
    relation_state: dict,
) -> None:
    workspace = relation_state["workspace"]
    evidence_a, evidence_b, _evidence_c = relation_state["evidence_ids"]
    hash_before = coverage_synthesis_input_hash(workspace, "q1")
    workspace["coverage_syntheses"] = [
        {
            "outcome_id": "synthesis:q1",
            "question_id": "q1",
            "input_revision": 4,
            "input_hash": hash_before,
            "status": "covered",
        }
    ]
    synthesis_before = deepcopy(workspace["coverage_syntheses"])

    result = await relation_state["handler"].update_source_finding_relations_tool(
        {
            "finding_id": "finding:S01:change",
            "source_relations": [_relation("S02", [evidence_a, evidence_b])],
        }
    )

    stored = relation_state["workspace"]
    finding = stored["source_findings"][0]
    assert result == {
        "accepted": True,
        "finding_id": "finding:S01:change",
        "changed": True,
        "relation_count": 1,
        "stale_outcome_question_ids": [],
    }
    assert finding["source_relations"][0]["relationship"] == "updates"
    assert finding["source_relations"][0]["related_source_id"] == relation_state["source_ids"][1]
    assert finding["semantic_revision"] == 4
    assert stored["triage_semantic_revision"] == 4
    assert stored["coverage_outcomes"][0]["is_stale"] is False
    assert stored["coverage_syntheses"] == synthesis_before
    assert coverage_synthesis_input_hash(stored, "q1") == hash_before
    assert set(stored["source_sets"][0]["evidence_ids"]) == {evidence_a, evidence_b}
    assert relation_state["evidence_toolset"].selection_calls[0]["evidence_ids"] == [
        evidence_a,
        evidence_b,
    ]
    inventory = relation_state["handler"].inventory
    materialized = materialize_source_map_v2(
        inventory.model_copy(deep=True),
        workspace=stored,
        inventory=inventory,
    )
    assert str(materialized.source_findings[0].source_relations[0].relationship) == "updates"


@pytest.mark.asyncio
async def test_finalizer_can_remove_an_unsupported_finding_relation(
    relation_state: dict,
) -> None:
    result = await relation_state["handler"].update_source_finding_relations_tool(
        {"finding_id": "finding:S01:change", "source_relations": []}
    )

    assert result["accepted"] is True
    assert result["changed"] is True
    assert relation_state["workspace"]["source_findings"][0]["source_relations"] == []
    assert relation_state["evidence_toolset"].selection_calls == []
    assert relation_state["workspace"]["coverage_outcomes"][0]["is_stale"] is False
    inventory = relation_state["handler"].inventory
    materialized = materialize_source_map_v2(
        inventory.model_copy(deep=True),
        workspace=relation_state["workspace"],
        inventory=inventory,
    )
    assert materialized.source_findings[0].source_relations == []


@pytest.mark.asyncio
async def test_finalizer_rejects_relation_evidence_from_only_one_side(
    relation_state: dict,
) -> None:
    evidence_a = relation_state["evidence_ids"][0]
    before = deepcopy(relation_state["workspace"]["source_findings"][0])

    result = await relation_state["handler"].update_source_finding_relations_tool(
        {
            "finding_id": "finding:S01:change",
            "source_relations": [_relation("S02", [evidence_a])],
        }
    )

    assert result["accepted"] is False
    assert result["error"] == (
        "A source relation requires grounded non-shell evidence from both sources"
    )
    assert result["missing_evidence_source_refs"] == ["S02"]
    assert relation_state["workspace"]["source_findings"][0] == before
    assert relation_state["save_count"] == 0
    assert relation_state["evidence_toolset"].selection_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("related_source_ref", "evidence_kind", "error"),
    [
        ("S01", "valid", "A source finding cannot relate its source to itself"),
        ("S99", "valid", "related_source_ref must belong to the current run corpus view"),
        ("internal_id", "valid", "related_source_ref must belong to the current run corpus view"),
        (
            "S02",
            "third_source",
            "Relation evidence_ids must belong to the finding source or related source in this run",
        ),
        (
            "S02",
            "other_run",
            "Relation evidence_ids must belong to the finding source or related source in this run",
        ),
    ],
)
async def test_finalizer_rejects_invalid_relation_scope_without_mutation(
    relation_state: dict,
    related_source_ref: str,
    evidence_kind: str,
    error: str,
) -> None:
    source_ids = relation_state["source_ids"]
    evidence_ids = relation_state["evidence_ids"]
    if related_source_ref == "internal_id":
        related_source_ref = source_ids[1]
    if evidence_kind == "valid":
        selected_evidence_ids = evidence_ids[:2]
    elif evidence_kind == "third_source":
        selected_evidence_ids = [evidence_ids[2]]
    else:
        selected_evidence_ids = [str(uuid.uuid4())]
    before = deepcopy(relation_state["workspace"]["source_findings"][0])

    result = await relation_state["handler"].update_source_finding_relations_tool(
        {
            "finding_id": "finding:S01:change",
            "source_relations": [_relation(related_source_ref, selected_evidence_ids)],
        }
    )

    assert result["accepted"] is False
    assert result["error"] == error
    assert relation_state["workspace"]["source_findings"][0] == before
    assert relation_state["save_count"] == 0


def test_relation_repair_tool_is_finalizer_only_and_has_one_atomic_job() -> None:
    async def handler(_args):
        return {}

    set_names = {
        tool.name
        for tool in source_triage_set_tools(handler, handler, handler)
    }
    finalizer_tools = source_triage_finalizer_tools(
        handler,
        handler,
        handler,
        handler,
        handler,
    )
    finalizer_names = {tool.name for tool in finalizer_tools}
    relation_tool = next(
        tool for tool in finalizer_tools if tool.name == "update_source_finding_relations"
    )

    assert "update_source_finding_relations" not in set_names
    assert "update_source_finding_relations" in finalizer_names
    assert relation_tool.parameters["required"] == ["finding_id", "source_relations"]
    assert "empty list removes unsupported relations" in relation_tool.description.lower()
    evidence_schema = relation_tool.parameters["properties"]["source_relations"]["items"][
        "properties"
    ]["evidence_ids"]
    assert evidence_schema["minItems"] == 2
    assert "at least one item from the finding's source" in evidence_schema["description"]
