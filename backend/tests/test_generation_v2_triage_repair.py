from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2.pipeline import source_triage_gate_repair_flow as repair_flow
from app.generation_v2.runtime_tool_output import (
    forbidden_tool_output_keys,
    model_facing_tool_output,
)
from app.generation_v2.schemas import (
    SourceCard,
    SourceCluster,
    SourceGapRecord,
    SourceMap,
    SourceRelationship,
    SourceTriageCompletion,
    SourceTriageGate,
    SourceTriageReport,
)
from app.generation_v2.source_triage_repair_state import carry_pending_triage_repair
from app.generation_v2.source_triage_repair_state import (
    reconcile_carried_triage_evidence,
    triage_workspace_evidence_ids,
)
from app.generation_v2.source_triage_workspace_repair import create_repair_sets_from_gate
from app.generation_v2.source_triage_workspace_prompting import workspace_summary_for_prompt
from app.generation_v2.source_triage_workspace_tools import (
    _canonical_section_record_parts,
    _full_source_map_repair_page,
)


def _inventory(count: int) -> SourceMap:
    return SourceMap(
        source_cards=[
            SourceCard(
                source_id=f"source-{index}",
                title=f"Source {index}",
                source_type="web",
                summary=f"Source {index} summary.",
            )
            for index in range(1, count + 1)
        ]
    )


def _set(index: int, *, status: str = "mapped") -> dict:
    return {
        "set_id": f"set_{index}",
        "label": f"Set {index}",
        "purpose": "Orient the assigned source.",
        "source_refs": [f"S{index:02d}"],
        "question_ids": ["q_main"],
        "status": status,
        "assessment_source_refs": [f"S{index:02d}"],
        "observations": [{"source_refs": [f"S{index:02d}"], "note": "Mapped."}],
    }


def test_new_attempt_carries_complete_context_and_reopens_only_reserved_set() -> None:
    first_set = {
        **_set(1),
        "evidence_ids": ["evidence-1"],
        "findings_summary": "First set is complete.",
    }
    repair_set = {
        **_set(2, status="in_progress"),
        "repair": True,
        "repair_targeted": True,
        "repair_source_refs": ["S02"],
        "repair_question_ids": ["q_main"],
        "evidence_ids": ["evidence-2"],
        "findings_summary": "Keep the durable part of the second set.",
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    assessments = [
        {"set_id": "set_1", "source_ref": "S01", "source_id": "source-1", "disposition": "anchor"},
        {
            "set_id": "set_2",
            "source_ref": "S02",
            "source_id": "source-2",
            "disposition": "supporting",
        },
    ]
    outcomes = [
        {"set_id": "set_1", "question_id": "q_main", "status": "covered", "evidence_ids": ["evidence-1"]},
        {"set_id": "set_2", "question_id": "q_main", "status": "partial", "evidence_ids": ["evidence-2"]},
    ]
    baseline_assessments = [
        {
            "set_id": "set_2",
            "source_ref": "S02",
            "evidence_ids": ["evidence-2"],
            "repair_baseline": True,
        }
    ]
    baseline_outcomes = [
        {
            "set_id": "set_2",
            "question_id": "q_main",
            "evidence_ids": ["evidence-2"],
            "repair_baseline": True,
        }
    ]
    existing = {
        "schema_version": "source_triage_workspace.v2",
        "manifest_hash": "same-corpus",
        "corpus_revision": 1,
        "attempt": 1,
        "source_order": ["S01", "S02"],
        "coverage_questions": [{"question_id": "q_main", "question": "What changed?"}],
        "source_sets": [first_set, repair_set],
        "source_assessments": assessments,
        "coverage_outcomes": outcomes,
        "repair_baseline_source_assessments": baseline_assessments,
        "repair_baseline_coverage_outcomes": baseline_outcomes,
        "processed_sets": [
            {"set_id": "set_1", "status": "mapped"},
            {"set_id": "set_2", "status": "mapped"},
        ],
        "mapped_sources": ["S01", "S02"],
        "deferred_sources": [],
        "source_map_finalization": {"ready": True, "summary": "Old attempt."},
        "repair_operation": {
            "status": "in_progress",
            "payload": {
                "repair_sets": [deepcopy(repair_set)],
                "repair_set_ids": ["set_2"],
            },
        },
    }
    fresh = {
        "schema_version": "source_triage_workspace.v2",
        "manifest_hash": "same-corpus",
        "corpus_revision": 1,
        "attempt": 2,
        "source_order": ["S01", "S02"],
        "coverage_questions": [],
        "source_sets": [],
        "source_assessments": [],
        "coverage_outcomes": [],
        "processed_sets": [],
    }

    carried = carry_pending_triage_repair(existing, fresh, source_order=["S01", "S02"])

    assert carried["attempt"] == 2
    assert carried["coverage_questions"] == existing["coverage_questions"]
    assert carried["source_assessments"] == assessments
    assert carried["coverage_outcomes"] == outcomes
    assert carried["repair_baseline_source_assessments"] == baseline_assessments
    assert carried["repair_baseline_coverage_outcomes"] == baseline_outcomes
    assert [item["set_id"] for item in carried["source_sets"]] == ["set_1", "set_2"]
    assert carried["source_sets"][0] == first_set
    reopened = carried["source_sets"][1]
    assert reopened["status"] == "planned"
    assert reopened["evidence_ids"] == ["evidence-2"]
    assert reopened["observations"] == repair_set["observations"]
    assert "started_at" not in reopened
    assert carried["processed_sets"] == [{"set_id": "set_1", "status": "mapped"}]
    assert carried["repair_operation"]["payload"]["repair_set_ids"] == ["set_2"]
    assert carried["repair_operation"]["payload"]["repair_sets"] == [reopened]
    assert "source_map_finalization" not in carried


def test_pending_repair_is_not_carried_into_a_different_corpus_revision() -> None:
    existing = {
        "corpus_revision": 1,
        "source_order": ["S01"],
        "repair_operation": {
            "status": "reserved",
            "payload": {"repair_set_ids": ["set_1"], "repair_sets": [_set(1)]},
        },
    }
    fresh = {"corpus_revision": 2, "attempt": 1, "source_order": ["S01"], "status": "orientation"}

    carried = carry_pending_triage_repair(existing, fresh, source_order=["S01"])

    assert carried is fresh
    assert carried["status"] == "orientation"
    assert "repair_operation" not in carried


def test_carried_evidence_is_remapped_or_reopened_without_stale_references() -> None:
    valid_old = str(uuid.uuid4())
    valid_new = str(uuid.uuid4())
    invalid_old = str(uuid.uuid4())
    existing = {
        "source_order": ["S01", "S02"],
        "mapped_sources": ["S01", "S02"],
        "deferred_sources": [{"source_ref": "S02", "reason": "Previously deferred."}],
        "source_sets": [
            {
                **_set(1),
                "evidence_ids": [valid_old],
                "observations": [{"source_refs": ["S01"], "evidence_ids": [valid_old]}],
            },
            {
                **_set(2),
                "evidence_ids": [invalid_old],
                "observations": [{"source_refs": ["S02"], "evidence_ids": [invalid_old]}],
            },
        ],
        "source_assessments": [
            {"set_id": "set_1", "source_ref": "S01", "evidence_ids": [valid_old]},
            {"set_id": "set_2", "source_ref": "S02", "evidence_ids": [invalid_old]},
        ],
        "coverage_outcomes": [
            {"set_id": "set_1", "question_id": "q_main", "evidence_ids": [valid_old]},
            {
                "set_id": "set_2",
                "question_id": "q_main",
                "source_refs": ["S02"],
                "evidence_ids": [invalid_old],
            },
        ],
        "processed_sets": [{"set_id": "set_1"}, {"set_id": "set_2"}],
        "repair_operation": {
            "status": "in_progress",
            "payload": {"repair_set_ids": ["set_1"], "repair_sets": []},
        },
    }

    reconciled = reconcile_carried_triage_evidence(
        existing,
        evidence_id_map={valid_old: valid_new},
        invalid_evidence_ids={invalid_old},
    )

    assert valid_old not in triage_workspace_evidence_ids(reconciled)
    assert invalid_old not in triage_workspace_evidence_ids(reconciled)
    assert valid_new in triage_workspace_evidence_ids(reconciled)
    assert [item["source_ref"] for item in reconciled["source_assessments"]] == ["S01"]
    assert [item["set_id"] for item in reconciled["coverage_outcomes"]] == ["set_1"]
    reopened = reconciled["source_sets"][1]
    assert reopened["status"] == "planned"
    assert reopened["repair_source_refs"] == ["S02"]
    assert reopened["repair_question_ids"] == ["q_main"]
    assert reopened["observations"] == []
    assert reopened["evidence_ids"] == []
    assert reconciled["mapped_sources"] == ["S01"]
    assert reconciled["deferred_sources"] == []
    assert reconciled["processed_sets"] == [{"set_id": "set_1"}]
    assert reconciled["repair_operation"]["payload"]["repair_set_ids"] == ["set_1", "set_2"]


def test_carried_repair_baselines_keep_semantics_and_remap_only_valid_evidence() -> None:
    valid_old = str(uuid.uuid4())
    valid_new = str(uuid.uuid4())
    invalid_old = str(uuid.uuid4())
    workspace = {
        "source_order": ["S01"],
        "source_sets": [
            {
                **_set(1, status="planned"),
                "repair": True,
                "repair_targeted": True,
                "repair_source_refs": ["S01"],
                "repair_question_ids": ["q_main"],
            }
        ],
        "source_assessments": [],
        "coverage_outcomes": [],
        "repair_baseline_source_assessments": [
            {
                "set_id": "set_1",
                "source_ref": "S01",
                "representation_reason": "Keep this old assessment detail.",
                "evidence_ids": [valid_old, invalid_old],
                "repair_baseline": True,
            }
        ],
        "repair_baseline_coverage_outcomes": [
            {
                "set_id": "set_1",
                "question_id": "q_main",
                "orientation_summary": "Keep this old outcome detail.",
                "source_refs": ["S01"],
                "evidence_ids": [valid_old, invalid_old],
                "repair_baseline": True,
            }
        ],
        "repair_operation": {
            "status": "in_progress",
            "payload": {"repair_set_ids": ["set_1"], "repair_sets": []},
        },
    }

    reconciled = reconcile_carried_triage_evidence(
        workspace,
        evidence_id_map={valid_old: valid_new},
        invalid_evidence_ids={invalid_old},
    )

    assessment = reconciled["repair_baseline_source_assessments"][0]
    outcome = reconciled["repair_baseline_coverage_outcomes"][0]
    assert assessment["representation_reason"] == "Keep this old assessment detail."
    assert outcome["orientation_summary"] == "Keep this old outcome detail."
    assert assessment["evidence_ids"] == [valid_new]
    assert outcome["evidence_ids"] == [valid_new]
    assert assessment["repair_baseline_evidence_unavailable"] is True
    assert outcome["repair_baseline_evidence_unavailable"] is True
    assert invalid_old not in triage_workspace_evidence_ids(reconciled)
    assert triage_workspace_evidence_ids(reconciled) == [valid_new]


@pytest.mark.asyncio
async def test_evidence_carry_requires_same_selected_immutable_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_module
    from app.models.generation_v2 import (
        EvidenceItem,
        EvidenceRetrievalChannel,
        EvidenceStrength,
        SourceRepresentationKind,
        SourceVersion,
        V2RecordStatus,
    )

    run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    selected_version_id = uuid.uuid4()
    stale_version_id = uuid.uuid4()
    selected_evidence_id = uuid.uuid4()
    stale_evidence_id = uuid.uuid4()
    carried_id = uuid.uuid4()

    def evidence(item_id: uuid.UUID, version_id: uuid.UUID) -> EvidenceItem:
        return EvidenceItem(
            id=item_id,
            generation_run_id=run_id,
            corpus_revision=1,
            attempt=1,
            project_id=project_id,
            source_id=source_id,
            source_version_id=version_id,
            retrieval_channel=EvidenceRetrievalChannel.MODEL_FILE_READ,
            snippet_text="Grounded source text.",
            normalized_quote_text="grounded source text.",
            locator_text="section 1",
            evidence_strength=EvidenceStrength.SNIPPET,
            snippet_hash=str(item_id),
            metadata_={"usage_status": "selected", "supports_query": False, "support_level": "weak"},
        )

    def version(version_id: uuid.UUID) -> SourceVersion:
        return SourceVersion(
            id=version_id,
            source_id=source_id,
            project_id=project_id,
            kind=SourceRepresentationKind.EXTRACTED_TEXT,
            status=V2RecordStatus.READY,
            upload_status=V2RecordStatus.READY,
            metadata_={},
        )

    class _Scalars:
        def __init__(self, values):
            self.values = values

        def __iter__(self):
            return iter(self.values)

    class _Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return _Scalars(self.values)

    class _Db:
        def __init__(self):
            self.results = [
                _Result(
                    [
                        evidence(selected_evidence_id, selected_version_id),
                        evidence(stale_evidence_id, stale_version_id),
                    ]
                ),
                _Result([version(selected_version_id), version(stale_version_id)]),
            ]

        async def execute(self, _statement):
            return self.results.pop(0)

        async def flush(self):
            return None

    persisted: list[dict] = []

    async def persist(_db, **kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(id=carried_id, metadata_={})

    monkeypatch.setattr(workspace_module, "persist_evidence_item", persist)
    mapping, invalid = await workspace_module._carry_evidence_to_current_attempt(
        _Db(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            revision=1,
            attempt=2,
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(selected_version_id)],
        ),
        workspace={"source_sets": [{"evidence_ids": [str(selected_evidence_id), str(stale_evidence_id)]}]},
        source_attempt=1,
    )

    assert mapping == {str(selected_evidence_id): str(carried_id)}
    assert invalid == {str(stale_evidence_id)}
    assert len(persisted) == 1
    assert persisted[0]["retrieval_channel"] == EvidenceRetrievalChannel.ATTEMPT_LINEAGE
    assert persisted[0]["metadata"]["origin_retrieval_channel"] == EvidenceRetrievalChannel.MODEL_FILE_READ
    lineage = persisted[0]["metadata"]["attempt_lineage"]
    assert lineage["parent_evidence_id"] == str(selected_evidence_id)
    assert lineage["source_version_id"] == str(selected_version_id)
    assert lineage["retrieval_performed"] is False


def test_carried_model_read_keeps_original_support_limitations() -> None:
    from app.generation_v2.tools.evidence_core_utils import unsupported_model_read_selected_as_support
    from app.models.generation_v2 import EvidenceItem, EvidenceRetrievalChannel

    item = EvidenceItem(
        retrieval_channel=EvidenceRetrievalChannel.ATTEMPT_LINEAGE,
        metadata_={
            "origin_retrieval_channel": EvidenceRetrievalChannel.MODEL_FILE_READ,
            "supports_query": False,
            "support_level": "weak",
        },
    )

    assert unsupported_model_read_selected_as_support(
        item,
        purpose="source_assessment",
        role="triage_source_assessment",
    )


def test_v2_repair_reopens_only_affected_existing_sets() -> None:
    inventory = _inventory(2)
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01", "S02"],
        "source_sets": [_set(1), _set(2)],
        "source_assessments": [{"source_ref": "S01", "source_id": "source-1"}],
        "coverage_outcomes": [],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Canonical workspace is incomplete.",
        workspace_violations=[
            {
                "code": "unassessed_sources",
                "message": "Every source needs one persisted assessment.",
                "source_refs": ["S02"],
            },
            {
                "code": "unresolved_coverage_questions",
                "message": "Every assigned question needs a final outcome.",
                "set_question_pairs": [["set_2", "q_main"]],
            },
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    assert len(repaired["source_sets"]) == 2
    assert repaired["source_sets"][0]["status"] == "mapped"
    target = repaired["source_sets"][1]
    assert target["set_id"] == "set_2"
    assert target["status"] == "planned"
    assert target["repair_targeted"] is True
    assert target["repair_source_refs"] == ["S02"]
    assert target["repair_question_ids"] == ["q_main"]
    assert {item["code"] for item in target["repair_targets"]} == {
        "unassessed_sources",
        "unresolved_coverage_questions",
    }
    assert [ref for item in repaired["source_sets"] for ref in item["source_refs"]] == ["S01", "S02"]
    assert repaired["repair_unaddressable_violations"] == []


def test_v2_repair_does_not_guess_when_source_assignment_needs_replanning() -> None:
    inventory = _inventory(1)
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01"],
        "source_sets": [_set(1), {**_set(1), "set_id": "set_duplicate"}],
        "source_assessments": [],
        "coverage_outcomes": [],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Source assignment is invalid.",
        workspace_violations=[
            {
                "code": "multiply_assigned_sources",
                "message": "A source cannot belong to more than one source set.",
                "source_refs": ["S01"],
            }
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    assert len(repaired["source_sets"]) == 2
    assert not any(item.get("repair_targeted") for item in repaired["source_sets"])
    assert [item["code"] for item in repaired["repair_unaddressable_violations"]] == ["multiply_assigned_sources"]


def test_v2_duplicate_assessments_are_rebuilt_in_their_existing_set() -> None:
    inventory = _inventory(1)
    first = {
        "source_ref": "S01",
        "source_id": "source-1",
        "processing_status": "resolved",
        "representation_reason": "First version.",
        "evidence_ids": ["evidence-first"],
    }
    second = {
        **first,
        "representation_reason": "Second version.",
        "evidence_ids": ["evidence-second"],
    }
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01"],
        "source_sets": [_set(1)],
        "source_assessments": [first, second],
        "coverage_outcomes": [],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Duplicate source assessment.",
        workspace_violations=[
            {
                "code": "duplicate_source_assessments",
                "message": "Every source needs exactly one current assessment.",
                "source_refs": ["S01"],
            }
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    assert repaired["source_assessments"] == []
    assert [
        item["source_ref"] for item in repaired["repair_baseline_source_assessments"]
    ] == ["S01", "S01"]
    assert [
        item["representation_reason"]
        for item in repaired["repair_baseline_source_assessments"]
    ] == ["First version.", "Second version."]
    assert [
        item["evidence_ids"] for item in repaired["repair_baseline_source_assessments"]
    ] == [["evidence-first"], ["evidence-second"]]
    assert [
        item["repair_baseline_version"]
        for item in repaired["repair_baseline_source_assessments"]
    ] == [1, 2]
    assert repaired["repair_baseline_source_assessments"][0]["repair_baseline"] is True
    assert repaired["source_sets"][0]["status"] == "planned"
    assert repaired["source_sets"][0]["repair_source_refs"] == ["S01"]


def test_v2_targeted_source_repair_preserves_findings_and_hints_as_baseline() -> None:
    inventory = _inventory(1)
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01"],
        "source_sets": [_set(1)],
        "source_assessments": [
            {
                "source_ref": "S01",
                "source_id": "source-1",
                "set_id": "set_1",
                "processing_status": "resolved",
                "finding_ids": ["finding:s01:1"],
                "evidence_ids": ["evidence-1"],
            }
        ],
        "source_findings": [
            {
                "finding_id": "finding:s01:1",
                "source_ref": "S01",
                "source_id": "source-1",
                "set_id": "set_1",
                "statement": "Old source memory that repair must be able to inspect.",
                "evidence_ids": ["evidence-1"],
                "possible_question_ids": ["q_main"],
            }
        ],
        "coverage_hints": [
            {
                "hint_id": "hint:s01:q_main",
                "source_ref": "S01",
                "set_id": "set_1",
                "question_id": "q_main",
                "candidate_finding_ids": ["finding:s01:1"],
                "candidate_evidence_ids": ["evidence-1"],
            }
        ],
        "coverage_outcomes": [],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Source assessment needs targeted repair.",
        workspace_violations=[
            {
                "code": "inconsistent_source_assessment",
                "message": "Assessment is invalid.",
                "source_refs": ["S01"],
            }
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    assert repaired["source_assessments"] == []
    assert repaired["source_findings"] == []
    assert repaired["coverage_hints"] == []
    assert repaired["repair_baseline_source_findings"][0]["repair_baseline"] is True
    assert repaired["repair_baseline_source_findings"][0]["statement"] == (
        "Old source memory that repair must be able to inspect."
    )
    assert repaired["repair_baseline_coverage_hints"][0]["repair_baseline"] is True
    assert repaired["repair_baseline_coverage_hints"][0]["candidate_finding_ids"] == ["finding:s01:1"]

    finding_values, _finding_parts = _canonical_section_record_parts(
        repaired,
        section="source_findings",
        current_set_id="set_1",
    )
    hint_values, _hint_parts = _canonical_section_record_parts(
        repaired,
        section="coverage_hints",
        current_set_id="set_1",
    )
    assert finding_values[0]["repair_baseline"] is True
    assert hint_values[0]["repair_baseline"] is True


def test_v2_synthesis_failures_reserve_finalizer_only_repair() -> None:
    inventory = _inventory(1)
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01"],
        "source_sets": [_set(1)],
        "source_assessments": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [{"question_id": "q_main", "status": "covered"}],
        "source_map_finalization": {"ready": True},
        "triage_semantic_revision": 4,
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Run-wide synthesis needs repair.",
        workspace_violations=[
            {
                "code": code,
                "message": "Run-wide synthesis is incomplete or invalid.",
                "question_ids": ["q_main"],
            }
            for code in (
                "duplicate_coverage_syntheses",
                "invalid_coverage_synthesis",
                "missing_coverage_syntheses",
                "unknown_coverage_syntheses",
            )
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    assert repaired["repair_unaddressable_violations"] == []
    assert not any(item.get("repair_targeted") for item in repaired["source_sets"])
    assert repaired["coverage_syntheses"] == []
    assert "source_map_finalization" not in repaired
    assert repaired["triage_semantic_revision"] == 5


def test_v2_structural_gate_failure_becomes_model_safe_finalizer_repair() -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    gap_id = "gap:internal-hash"
    inventory = SourceMap(
        source_cards=[
            SourceCard(source_id=source_id_1, title="First", source_type="web", summary="First."),
            SourceCard(source_id=source_id_2, title="Second", source_type="web", summary="Second."),
        ]
    )
    source_map = SourceMap(
        source_triage=SourceTriageReport(
            source_relationships=[
                SourceRelationship(
                    source_id=source_id_1,
                    related_source_id=source_id_2,
                    relationship="duplicate",
                )
            ],
            source_clusters=[
                SourceCluster(
                    cluster_id="cluster_1",
                    label="Cluster",
                    source_ids=[source_id_1, source_id_2],
                )
            ],
        ),
        gap_records=[
            SourceGapRecord(
                gap_id=gap_id,
                description="A persisted gap.",
                source_ids=[source_id_1],
            )
        ],
    )
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01", "S02"],
        "source_sets": [_set(1), _set(2)],
        "source_assessments": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [{"question_id": "q_main", "status": "covered"}],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Invalid SourceMap structure.",
        ungrounded_relationships=[
            f"relationship:evidence_scope:{source_id_1}:duplicate:{source_id_2}",
            "cluster:role_overlap:cluster_1",
            f"gap:evidence_scope:{gap_id}",
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=source_map,
    )

    assert repaired["repair_unaddressable_violations"] == []
    assert not any(item.get("repair_targeted") for item in repaired["source_sets"])
    assert repaired["coverage_syntheses"] == []
    repairs = repaired["source_map_repair_items"]
    assert [item["kind"] for item in repairs] == ["relationship", "cluster", "gap"]
    assert repairs[0]["source_refs"] == ["S01", "S02"]
    assert repairs[1]["source_refs"] == ["S01", "S02"]
    assert repairs[2]["gap_ref"].startswith("G-")
    serialized = str(repairs)
    assert source_id_1 not in serialized
    assert source_id_2 not in serialized
    assert gap_id not in serialized
    assert "target_fingerprint" not in serialized

    page = _full_source_map_repair_page(repaired, cursor=0, limit=10)
    summary = workspace_summary_for_prompt(repaired)
    for model_payload in (page, summary):
        serialized_payload = str(model_payload)
        assert forbidden_tool_output_keys(model_payload) == set()
        assert source_id_1 not in serialized_payload
        assert source_id_2 not in serialized_payload
        assert gap_id not in serialized_payload
        assert "target_fingerprint" not in serialized_payload
    assert model_facing_tool_output("list_triage_source_sets", page) is page


def test_legacy_repair_prompt_does_not_expose_internal_gate_ids() -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            SourceCard(source_id=source_id_1, title="First", source_type="web", summary="First."),
            SourceCard(source_id=source_id_2, title="Second", source_type="web", summary="Second."),
        ]
    )
    source_map = SourceMap(
        source_triage=SourceTriageReport(
            source_relationships=[
                SourceRelationship(
                    source_id=source_id_1,
                    related_source_id=source_id_2,
                    relationship="duplicate",
                )
            ]
        )
    )
    gate = SourceTriageGate(
        passed=False,
        summary="Repair the failed gate.",
        invalid_evidence_ids=[evidence_id],
        ungrounded_relationships=[f"{source_id_1}:duplicate:{source_id_2}"],
    )

    repaired = create_repair_sets_from_gate(
        {"source_order": ["S01", "S02"], "source_sets": []},
        gate=gate,
        inventory=inventory,
        source_map=source_map,
    )

    serialized = str(repaired["source_sets"])
    assert source_id_1 not in serialized
    assert source_id_2 not in serialized
    assert evidence_id not in serialized
    assert set(repaired["source_sets"][0]["source_refs"]) == {"S01", "S02"}


def test_cross_set_invalid_relationship_evidence_routes_to_finalizer_not_set_agents() -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            SourceCard(source_id=source_id_1, title="First", source_type="web", summary="First."),
            SourceCard(source_id=source_id_2, title="Second", source_type="web", summary="Second."),
        ]
    )
    source_map = SourceMap(
        source_triage=SourceTriageReport(
            source_relationships=[
                SourceRelationship(
                    source_id=source_id_1,
                    related_source_id=source_id_2,
                    relationship="duplicate",
                    evidence_ids=[evidence_id],
                    rationale="The old trace used invalid evidence.",
                )
            ]
        )
    )
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01", "S02"],
        "source_sets": [_set(1), _set(2)],
        "source_assessments": [],
        "coverage_outcomes": [],
    }

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=SourceTriageGate(
            passed=False,
            summary="Invalid structural evidence.",
            invalid_evidence_ids=[evidence_id],
        ),
        inventory=inventory,
        source_map=source_map,
    )

    assert repaired["repair_unaddressable_violations"] == []
    assert not any(item.get("repair_targeted") for item in repaired["source_sets"])
    assert repaired["source_map_repair_items"][0]["issues"] == ["invalid_evidence_reference"]
    assert repaired["source_map_repair_items"][0]["source_refs"] == ["S01", "S02"]


@pytest.mark.asyncio
async def test_synthesis_only_gate_failure_reruns_finalizer_without_reopening_source_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(1)
    stored = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01"],
        "source_sets": [_set(1)],
        "source_assessments": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [],
        "mapped_sources": ["S01"],
        "deferred_sources": [],
    }
    finalizer_calls = 0
    executed_sets: list[str] = []

    async def load_workspace(_db, _run_id):
        return stored

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        payload = deepcopy(workspace)
        stored.clear()
        stored.update(payload)
        return SimpleNamespace(payload=deepcopy(stored))

    async def run_set(**kwargs):
        executed_sets.append(str(kwargs["source_set"]["set_id"]))
        return SourceTriageCompletion(ready=True, summary="Unexpected set repair.", notes=[])

    async def build_map(*_args, **_kwargs):
        return SourceMap()

    async def finalize(*_args, **_kwargs):
        nonlocal finalizer_calls
        finalizer_calls += 1
        return SourceTriageCompletion(ready=True, summary="Syntheses rebuilt.", notes=[])

    async def validate(*_args, **_kwargs):
        return SourceTriageGate(passed=True, summary="Passed.")

    async def materialize_deferrals(*, db, run, workspace):
        del db, run
        return workspace

    async def save_pre_repair(**_kwargs):
        return None

    monkeypatch.setattr(repair_flow, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(repair_flow, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(repair_flow, "_run_source_triage_set", run_set)
    monkeypatch.setattr(repair_flow, "build_source_map_from_write_actions", build_map)
    monkeypatch.setattr(repair_flow, "build_source_triage_finalizer_agent", finalize)
    monkeypatch.setattr(repair_flow, "validate_source_map_gate", validate)
    monkeypatch.setattr(repair_flow, "_save_pre_repair_gate_artifacts", save_pre_repair)

    class _DB:
        async def commit(self):
            return None

    result = await repair_flow.run_source_triage_gate_repair(
        db=_DB(),  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-id"),
        runtime=SimpleNamespace(),
        corpus=SimpleNamespace(run_view=SimpleNamespace()),
        evidence_toolset=SimpleNamespace(),
        source_inventory=inventory,
        source_map=SourceMap(),
        workspace=stored,
        triage_gate=SourceTriageGate(
            passed=False,
            summary="One run-wide synthesis is missing.",
            workspace_violations=[
                {
                    "code": "missing_coverage_syntheses",
                    "message": "Every question needs a run-wide synthesis.",
                    "question_ids": ["q_main"],
                }
            ],
        ),
        triage_completion=SourceTriageCompletion(ready=False, summary="Initial failure.", notes=[]),
        triage_completion_artifact_id="completion-id",
        finalizer_tools=[],
        unresolved_completion_handler=finalize,
        materialize_deferrals_handler=materialize_deferrals,
    )

    assert executed_sets == []
    assert finalizer_calls == 1
    assert result[2].passed is True
    assert stored["repair_operation"]["status"] == "completed"


def test_v2_deferral_consistency_failure_repairs_only_its_existing_set() -> None:
    inventory = _inventory(1)
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01"],
        "source_sets": [_set(1)],
        "source_assessments": [
            {
                "set_id": "set_1",
                "source_ref": "S01",
                "source_id": "source-1",
                "disposition": "deferred",
            }
        ],
        "coverage_outcomes": [
            {
                "set_id": "set_1",
                "question_id": "q_main",
                "status": "covered",
                "source_refs": ["S01"],
            }
        ],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Deferral state is contradictory.",
        workspace_violations=[
            {
                "code": "deferred_source_index_mismatch",
                "message": "Deferred index is stale.",
                "source_refs": ["S01"],
            },
            {
                "code": "deferred_source_active_outcome",
                "message": "Deferred source is active in an outcome.",
                "source_refs": ["S01"],
                "set_question_pairs": [["set_1", "q_main"]],
            },
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    assert repaired["source_assessments"] == workspace["source_assessments"]
    assert repaired["coverage_outcomes"] == []
    assert repaired["repair_baseline_coverage_outcomes"][0]["question_id"] == "q_main"
    assert repaired["repair_baseline_coverage_outcomes"][0]["repair_baseline"] is True
    assert repaired["source_sets"][0]["status"] == "planned"
    assert repaired["source_sets"][0]["repair_source_refs"] == ["S01"]
    assert repaired["source_sets"][0]["repair_question_ids"] == ["q_main"]
    assert repaired["repair_unaddressable_violations"] == []


def test_v2_repair_targets_new_assessment_outcome_and_deferral_violations() -> None:
    inventory = _inventory(2)
    workspace = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": ["S01", "S02"],
        "source_sets": [_set(1), {**_set(2), "status": "deferred", "deferred_reason": ""}],
        "source_assessments": [
            {
                "source_ref": "S01",
                "source_id": "source-1",
                "set_id": "set_1",
                "representation_status": "unchecked",
            },
            {
                "source_ref": "S02",
                "source_id": "source-2",
                "set_id": "set_1",
                "representation_status": "readable",
            },
        ],
        "coverage_outcomes": [
            {"set_id": "set_1", "question_id": "q_main", "status": "covered"},
            {"set_id": "set_1", "question_id": "q_main", "status": "covered"},
            {"set_id": "set_2", "question_id": "q_main", "status": "missing"},
        ],
    }
    gate = SourceTriageGate(
        passed=False,
        summary="Canonical workspace needs targeted repair.",
        workspace_violations=[
            {
                "code": "unchecked_source_representations",
                "message": "Representation must be checked.",
                "source_refs": ["S01"],
            },
            {
                "code": "source_assessment_set_mismatch",
                "message": "Assessment belongs to the wrong set.",
                "source_refs": ["S02"],
            },
            {
                "code": "duplicate_coverage_outcomes",
                "message": "Outcome must be unique.",
                "set_question_pairs": [["set_1", "q_main"]],
            },
            {
                "code": "incomplete_coverage_outcome",
                "message": "Outcome is incomplete.",
                "set_question_pairs": [["set_2", "q_main"]],
            },
            {
                "code": "unexplained_deferred_source_set",
                "message": "Deferral needs a reason.",
                "set_id": "set_2",
            },
        ],
    )

    repaired = create_repair_sets_from_gate(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=SourceMap(),
    )

    targets = {item["set_id"]: item for item in repaired["source_sets"]}
    assert targets["set_1"]["repair_source_refs"] == ["S01"]
    assert targets["set_1"]["repair_question_ids"] == ["q_main"]
    assert targets["set_2"]["repair_source_refs"] == ["S02"]
    assert targets["set_2"]["repair_question_ids"] == ["q_main"]
    assert repaired["source_assessments"] == [workspace["source_assessments"][0]]
    assert repaired["coverage_outcomes"] == []
    assert [
        item["source_ref"] for item in repaired["repair_baseline_source_assessments"]
    ] == ["S02"]
    assessment_baseline = repaired["repair_baseline_source_assessments"][0]
    assert assessment_baseline["set_id"] == "set_2"
    assert assessment_baseline["original_set_id"] == "set_1"
    assert {
        (item["set_id"], item["question_id"])
        for item in repaired["repair_baseline_coverage_outcomes"]
    } == {("set_1", "q_main"), ("set_2", "q_main")}
    set_1_baselines = [
        item
        for item in repaired["repair_baseline_coverage_outcomes"]
        if item["set_id"] == "set_1"
    ]
    assert len(set_1_baselines) == 2
    assert [item["repair_baseline_version"] for item in set_1_baselines] == [1, 2]
    assert repaired["repair_unaddressable_violations"] == []


@pytest.mark.asyncio
async def test_repair_flow_runs_every_reserved_existing_set_and_materializes_latest_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory(5)
    repair_sets = [{**_set(index, status="planned"), "repair": True} for index in range(1, 6)]
    stored = {
        "schema_version": "source_triage_workspace.v2",
        "source_order": [f"S{index:02d}" for index in range(1, 6)],
        "source_sets": repair_sets,
        "source_assessments": [],
        "coverage_outcomes": [],
        "mapped_sources": [],
        "deferred_sources": [],
        "repair_operation": {
            "status": "reserved",
            "payload": {
                "completion": {"ready": False, "summary": "Initial failure.", "notes": []},
                "repair_sets": deepcopy(repair_sets),
                "repair_set_ids": [item["set_id"] for item in repair_sets],
            },
        },
    }
    executed: list[str] = []
    materialized_statuses: list[list[str]] = []

    async def load_workspace(_db, _run_id):
        return stored

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        payload = deepcopy(workspace)
        stored.clear()
        stored.update(payload)
        return SimpleNamespace(payload=deepcopy(stored))

    async def run_set(**kwargs):
        set_id = str(kwargs["source_set"]["set_id"])
        executed.append(set_id)
        for source_set in stored["source_sets"]:
            if source_set["set_id"] == set_id:
                source_set["status"] = "mapped"
        source_ref = str(kwargs["source_set"]["source_refs"][0])
        stored["mapped_sources"].append(source_ref)
        return SourceTriageCompletion(ready=True, summary=f"Repaired {set_id}.", notes=[])

    async def build_map(*_args, **_kwargs):
        return SourceMap()

    def materialize(source_map, *, workspace, inventory):
        del inventory
        materialized_statuses.append([str(item["status"]) for item in workspace["source_sets"]])
        return source_map

    async def finalize(*_args, **_kwargs):
        return SourceTriageCompletion(ready=True, summary="Finalized.", notes=[])

    async def validate(*_args, **_kwargs):
        return SourceTriageGate(passed=True, summary="Passed.")

    async def materialize_deferrals(*, db, run, workspace):
        del db, run
        return workspace

    monkeypatch.setattr(repair_flow, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(repair_flow, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(repair_flow, "_run_source_triage_set", run_set)
    monkeypatch.setattr(repair_flow, "build_source_map_from_write_actions", build_map)
    monkeypatch.setattr(repair_flow, "materialize_source_map_v2", materialize)
    monkeypatch.setattr(repair_flow, "build_source_triage_finalizer_agent", finalize)
    monkeypatch.setattr(repair_flow, "validate_source_map_gate", validate)

    class _DB:
        async def commit(self):
            return None

    result = await repair_flow.run_source_triage_gate_repair(
        db=_DB(),  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-id"),
        runtime=SimpleNamespace(),
        corpus=SimpleNamespace(run_view=SimpleNamespace()),
        evidence_toolset=SimpleNamespace(),
        source_inventory=inventory,
        source_map=SourceMap(),
        workspace=stored,
        triage_gate=SourceTriageGate(passed=False, summary="Initial failure."),
        triage_completion=SourceTriageCompletion(ready=False, summary="Initial failure.", notes=[]),
        triage_completion_artifact_id="completion-id",
        finalizer_tools=[],
        unresolved_completion_handler=finalize,
        materialize_deferrals_handler=materialize_deferrals,
    )

    assert executed == [f"set_{index}" for index in range(1, 6)]
    assert materialized_statuses == [["mapped"] * 5, ["mapped"] * 5]
    assert result[2].passed is True
    assert stored["repair_operation"]["status"] == "completed"
