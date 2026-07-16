from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2 import research_workspace as research_workspace_module
from app.generation_v2.schemas import ResearchHandoffItem, SourceCard, SourceMap
from app.generation_v2.source_triage_canonical import (
    aggregate_coverage_outcomes,
    audit_corpus_brief,
    canonical_workspace_violations,
    coverage_question_material_lineage,
    coverage_synthesis_input_hash,
    materialize_corpus_brief,
    materialize_source_map_v2,
)
from app.generation_v2.source_triage_workspace_capsules import capsule_cache_key


def _card(source_id: str, evidence_ids: list[str]) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[f"version:{source_id}"],
        evidence_ids=evidence_ids,
        title=f"Source {source_id}",
        source_type="web",
        reliability="high",
        summary=f"Neutral summary for {source_id}",
    )


def _assessment(
    source_ref: str,
    source_id: str,
    evidence_ids: list[str],
    *,
    processing_status: str = "resolved",
    representation_status: str = "readable",
    relevance: str = "core",
    authority: str = "official",
    uniqueness: str = "unique",
    disposition: str = "anchor",
) -> dict:
    return {
        "assessment_id": f"assessment:{source_id}",
        "source_ref": source_ref,
        "source_id": source_id,
        "source_version_ids": [f"version:{source_id}"],
        "processing_status": processing_status,
        "representation_status": representation_status,
        "representation_reason": f"Representation is {representation_status}.",
        "relevance": relevance,
        "relevance_reason": "Directly relevant to the assigned coverage question.",
        "authority": authority,
        "authority_reason": "Published by the responsible institution.",
        "uniqueness": uniqueness,
        "uniqueness_reason": "Contains a distinct part of the record.",
        "disposition": disposition,
        "coverage_question_ids": ["q_main"],
        "editorial_functions": ["mechanism"],
        "evidence_ids": evidence_ids,
        "limitations": [],
        "confidence": 0.9,
    }


def _workspace(
    assessments: list[dict],
    *,
    source_sets: list[dict] | None = None,
    outcomes: list[dict] | None = None,
    question_text: str = "How does the mechanism work?",
) -> dict:
    source_order = [str(item["source_ref"]) for item in assessments]
    source_ids = [str(item["source_id"]) for item in assessments]
    evidence_ids = [str(value) for item in assessments for value in item.get("evidence_ids") or []]
    return {
        "schema_version": "source_triage_workspace.v2",
        "intent": {
            "user_prompt": "Explain the institutional mechanism.",
            "language": "en",
            "duration_seconds": 2_700,
        },
        "source_order": source_order,
        "coverage_questions": [
            {
                "question_id": "q_main",
                "question": question_text,
                "kind": "mechanism",
                "importance": "critical",
                "rationale": "The user's promise depends on this mechanism.",
                "origin": "planned",
            }
        ],
        "source_sets": source_sets
        if source_sets is not None
        else [
            {
                "set_id": "set_main",
                "label": "Mechanism",
                "purpose": "Map the mechanism sources.",
                "rationale": "These sources must be compared.",
                "source_refs": source_order,
                "question_ids": ["q_main"],
                "priority": "critical",
                "status": "mapped",
                "findings_summary": "The group has been mapped.",
                "evidence_ids": evidence_ids,
            }
        ],
        "source_assessments": assessments,
        "coverage_outcomes": outcomes
        if outcomes is not None
        else [
            {
                "set_id": "set_main",
                "question_id": "q_main",
                "status": "covered",
                "source_ids": source_ids,
                "evidence_ids": evidence_ids,
                "orientation_summary": "The sources describe the mechanism.",
                "confidence": 0.8,
                "next_action": "research",
            }
        ],
        "source_map_finalization": {"summary": "Canonical triage handoff."},
    }


def _materialized_map(workspace: dict) -> SourceMap:
    cards = [
        _card(str(item["source_id"]), [str(value) for value in item.get("evidence_ids") or []])
        for item in workspace["source_assessments"]
    ]
    inventory = SourceMap(source_cards=cards)
    return materialize_source_map_v2(
        SourceMap(source_cards=cards),
        workspace=workspace,
        inventory=inventory,
    )


def _materialized_brief(workspace: dict, source_map: SourceMap):
    return materialize_corpus_brief(
        generation_run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        run_corpus_view_id=str(uuid.uuid4()),
        corpus_revision=2,
        attempt=3,
        source_map_artifact_id=str(uuid.uuid4()),
        source_map=source_map,
        workspace=workspace,
    )


def test_canonical_workspace_requires_exactly_one_assessment_per_source() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    workspace = _workspace([first, second])
    workspace["source_assessments"] = [first, dict(first)]

    violations = canonical_workspace_violations(workspace)
    by_code = {item["code"]: item for item in violations}

    assert by_code["duplicate_source_assessments"]["source_refs"] == ["S01"]
    assert by_code["unassessed_sources"]["source_refs"] == ["S02"]


def test_canonical_workspace_requires_outcome_for_each_set_question_pair() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    source_sets = [
        {
            "set_id": "set_1",
            "source_refs": ["S01"],
            "question_ids": ["q_main"],
            "status": "mapped",
        },
        {
            "set_id": "set_2",
            "source_refs": ["S02"],
            "question_ids": ["q_main"],
            "status": "mapped",
        },
    ]
    outcomes = [
        {
            "set_id": "set_1",
            "question_id": "q_main",
            "status": "covered",
        }
    ]
    workspace = _workspace([first, second], source_sets=source_sets, outcomes=outcomes)

    violations = canonical_workspace_violations(workspace)
    unresolved = next(item for item in violations if item["code"] == "unresolved_coverage_questions")

    assert unresolved["set_question_pairs"] == [["set_2", "q_main"]]


def test_canonical_workspace_blocks_unfinished_set_and_source_processing() -> None:
    assessment = _assessment(
        "S01",
        "source-1",
        ["evidence-1"],
        processing_status="in_progress",
    )
    workspace = _workspace([assessment])
    workspace["source_sets"][0]["status"] = "in_progress"

    codes = {item["code"] for item in canonical_workspace_violations(workspace)}

    assert "unfinished_source_set" in codes
    assert "unfinished_source_assessments" in codes


def test_canonical_workspace_rejects_covered_outcome_without_orientation() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    workspace["coverage_outcomes"][0]["orientation_summary"] = ""

    codes = {item["code"] for item in canonical_workspace_violations(workspace)}

    assert "incomplete_coverage_outcome" in codes


def test_finalization_requires_one_coherent_run_wide_synthesis_per_question() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])

    missing_codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }
    workspace["coverage_syntheses"] = [
        {
            "outcome_id": "synthesis:q_main",
            "question_id": "q_main",
            "status": "covered",
            "source_refs": ["S01"],
            "source_ids": ["source-1"],
            "evidence_ids": ["evidence-1"],
            "orientation_summary": "The run-wide evidence establishes the mechanism.",
            "missing_detail": "The same mechanism is still absent.",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.9,
            "next_action": "research",
        }
    ]
    workspace["coverage_syntheses"][0]["input_revision"] = 0
    workspace["coverage_syntheses"][0]["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")
    contradictory_codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }

    assert "missing_coverage_syntheses" in missing_codes
    assert "invalid_coverage_synthesis" in contradictory_codes


def test_deferred_outcome_and_synthesis_require_a_checked_representation_blocker() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    workspace["coverage_outcomes"] = [
        {
            "outcome_id": "outcome:q_main:set_main",
            "set_id": "set_main",
            "question_id": "q_main",
            "status": "deferred",
            "source_refs": [],
            "source_ids": [],
            "evidence_ids": [],
            "orientation_summary": "",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "The representation blocks evaluation.",
            "confidence": 0.5,
            "next_action": "omit",
        }
    ]
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "deferred",
        "source_refs": [],
        "source_ids": [],
        "evidence_ids": [],
        "orientation_summary": "",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "The representation blocks evaluation.",
        "confidence": 0.5,
        "next_action": "omit",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")

    readable_codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }
    workspace["source_assessments"][0]["representation_status"] = "partial"
    workspace["source_assessments"][0]["representation_reason"] = "Only part of the source is readable."
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")
    partial_codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }
    workspace["source_assessments"][0].update(
        {
            "representation_status": "not_target_content",
            "representation_reason": "Only a service page was obtained.",
            "relevance": "unknown",
            "relevance_reason": "",
            "authority": "unknown",
            "authority_reason": "",
            "uniqueness": "unknown",
            "uniqueness_reason": "",
            "disposition": "deferred",
            "disposition_reason": "The requested source content was not obtained.",
            "coverage_question_ids": [],
            "editorial_functions": [],
        }
    )
    workspace["deferred_sources"] = [
        {"source_ref": "S01", "reason": "The requested source content was not obtained."}
    ]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")
    not_target_codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }
    workspace["source_assessments"][0]["uniqueness"] = "unique"
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")
    contradictory_codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }

    assert "incomplete_coverage_outcome" in readable_codes
    assert "invalid_coverage_synthesis" in readable_codes
    assert "invalid_coverage_synthesis" not in partial_codes
    assert "incomplete_coverage_outcome" not in partial_codes
    assert "incomplete_coverage_outcome" not in not_target_codes
    assert "invalid_coverage_synthesis" not in not_target_codes
    assert "inconsistent_source_assessment" in contradictory_codes


def test_coverage_synthesis_rejects_source_and_evidence_without_question_lineage() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    first["set_id"] = "set_main"
    second["set_id"] = "set_main"
    second["coverage_question_ids"] = ["q_other"]
    source_sets = [
        {
            "set_id": "set_main",
            "label": "Shared set",
            "purpose": "Answer two different questions without mixing their material.",
            "source_refs": ["S01", "S02"],
            "question_ids": ["q_main", "q_other"],
            "status": "mapped",
            "findings_summary": "Both sources were mapped for their respective questions.",
            "evidence_ids": ["evidence-1", "evidence-2"],
        },
    ]
    outcomes = [
        {
            "outcome_id": "outcome:q_main:set_main",
            "set_id": "set_main",
            "question_id": "q_main",
            "status": "covered",
            "source_refs": ["S01", "S02"],
            "source_ids": ["source-1", "source-2"],
            "evidence_ids": ["evidence-1"],
            "orientation_summary": "The first source answers the main question.",
            "next_action": "research",
        },
        {
            "outcome_id": "outcome:q_other:set_main",
            "set_id": "set_main",
            "question_id": "q_other",
            "status": "covered",
            "source_refs": ["S01", "S02"],
            "source_ids": ["source-1", "source-2"],
            "evidence_ids": ["evidence-2"],
            "orientation_summary": "The second source answers only the other question.",
            "next_action": "research",
        },
    ]
    workspace = _workspace([first, second], source_sets=source_sets, outcomes=outcomes)
    workspace["coverage_questions"].append(
        {
            "question_id": "q_other",
            "question": "What does the other source establish?",
            "kind": "context",
            "importance": "supporting",
        }
    )
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "covered",
        "source_refs": ["S02"],
        "source_ids": ["source-2"],
        "evidence_ids": ["evidence-2"],
        "orientation_summary": "Unrelated material is presented as the main answer.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
        "next_action": "research",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")

    violation = next(
        item
        for item in canonical_workspace_violations(workspace, require_syntheses=True)
        if item["code"] == "invalid_coverage_synthesis"
    )

    assert violation["source_refs"] == []
    assert violation["evidence_ids"] == ["evidence-2"]


def test_coverage_synthesis_hash_tracks_every_question_linked_assessment() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    first["set_id"] = "set_main"
    second["set_id"] = "set_other"
    second["coverage_question_ids"] = ["q_main"]
    workspace = _workspace(
        [first, second],
        source_sets=[
            {
                "set_id": "set_main",
                "source_refs": ["S01"],
                "question_ids": ["q_main"],
                "status": "mapped",
            },
            {
                "set_id": "set_other",
                "source_refs": ["S02"],
                "question_ids": [],
                "status": "mapped",
            },
        ],
    )

    before = coverage_synthesis_input_hash(workspace, "q_main")
    second["authority_reason"] = "The question-linked source assessment changed."
    after = coverage_synthesis_input_hash(workspace, "q_main")

    assert before != after


def test_coverage_synthesis_hash_ignores_other_question_assessment_in_shared_set() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    second["coverage_question_ids"] = ["q_other"]
    source_set = {
        "set_id": "set_main",
        "source_refs": ["S01", "S02"],
        "question_ids": ["q_main", "q_other"],
        "status": "mapped",
    }
    workspace = _workspace(
        [first, second],
        source_sets=[source_set],
        outcomes=[
            {
                "set_id": "set_main",
                "question_id": "q_main",
                "status": "covered",
                "source_ids": ["source-1", "source-2"],
                "evidence_ids": ["evidence-1"],
                "orientation_summary": "The first source answers q_main.",
                "next_action": "research",
            }
        ],
    )
    workspace["coverage_questions"].append(
        {"question_id": "q_other", "question": "What answers the other question?"}
    )

    before = coverage_synthesis_input_hash(workspace, "q_main")
    second["authority_reason"] = "Unrelated q_other assessment changed."
    after = coverage_synthesis_input_hash(workspace, "q_main")

    assert before == after


def test_question_material_lineage_uses_set_assignment_only_for_legacy_assessment() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    assessment.pop("coverage_question_ids")
    workspace = _workspace([assessment], outcomes=[])

    legacy_lineage = coverage_question_material_lineage(workspace, "q_main")
    assessment["coverage_question_ids"] = []
    explicit_lineage = coverage_question_material_lineage(workspace, "q_main")

    assert legacy_lineage == {"source_refs": ["S01"], "evidence_ids": ["evidence-1"]}
    assert explicit_lineage == {"source_refs": [], "evidence_ids": []}


def test_corpus_brief_uses_run_wide_synthesis_instead_of_concatenating_set_gaps() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    source_sets = [
        {
            "set_id": "set_1",
            "label": "First",
            "source_refs": ["S01"],
            "question_ids": ["q_main"],
            "status": "mapped",
            "evidence_ids": ["evidence-1"],
        },
        {
            "set_id": "set_2",
            "label": "Second",
            "source_refs": ["S02"],
            "question_ids": ["q_main"],
            "status": "mapped",
            "evidence_ids": ["evidence-2"],
        },
    ]
    outcomes = [
        {
            "outcome_id": "outcome:q_main:set_1",
            "set_id": "set_1",
            "question_id": "q_main",
            "status": "partial",
            "source_refs": ["S01"],
            "source_ids": ["source-1"],
            "evidence_ids": ["evidence-1"],
            "orientation_summary": "The first set establishes the institution.",
            "missing_detail": "This set does not contain the later crisis.",
            "confidence": 0.8,
            "next_action": "research",
        },
        {
            "outcome_id": "outcome:q_main:set_2",
            "set_id": "set_2",
            "question_id": "q_main",
            "status": "covered",
            "source_refs": ["S02"],
            "source_ids": ["source-2"],
            "evidence_ids": ["evidence-2"],
            "orientation_summary": "The second set supplies the later crisis.",
            "confidence": 0.9,
            "next_action": "research",
        },
    ]
    workspace = _workspace([first, second], source_sets=source_sets, outcomes=outcomes)
    workspace["coverage_syntheses"] = [
        {
            "outcome_id": "synthesis:q_main",
            "question_id": "q_main",
            "status": "covered",
            "source_refs": ["S01", "S02"],
            "source_ids": ["source-1", "source-2"],
            "evidence_ids": ["evidence-1", "evidence-2"],
            "orientation_summary": "Together the sets establish both the institution and the later crisis.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "confidence": 0.85,
            "next_action": "research",
        }
    ]
    workspace["coverage_syntheses"][0]["input_revision"] = 0
    workspace["coverage_syntheses"][0]["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")
    source_map = _materialized_map(workspace)

    brief = _materialized_brief(workspace, source_map)

    assert len(brief.coverage_outcomes) == 1
    assert str(brief.coverage_outcomes[0].status) == "covered"
    assert brief.coverage_outcomes[0].orientation_summary == (
        "Together the sets establish both the institution and the later crisis."
    )
    assert "does not contain" not in brief.coverage_outcomes[0].missing_detail


def test_legacy_coverage_aggregation_preserves_zero_confidence() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    workspace["coverage_outcomes"][0]["confidence"] = 0.0

    outcomes = aggregate_coverage_outcomes(workspace)

    assert outcomes[0].confidence == 0.0


def test_stale_run_wide_synthesis_cannot_override_a_changed_per_set_outcome() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "covered",
        "source_refs": ["S01"],
        "source_ids": ["source-1"],
        "evidence_ids": ["evidence-1"],
        "orientation_summary": "The source establishes the mechanism.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.9,
        "next_action": "research",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")
    workspace["coverage_outcomes"][0].update(
        {
            "status": "missing",
            "source_ids": [],
            "evidence_ids": [],
            "orientation_summary": "",
            "missing_detail": "The mechanism is not established by this set.",
            "next_action": "source_acquisition",
        }
    )

    codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }

    assert "invalid_coverage_synthesis" in codes
    with pytest.raises(ValueError, match="incomplete or stale"):
        _materialized_brief(workspace, _materialized_map(workspace))


def test_run_wide_synthesis_cannot_drop_source_ids_for_its_source_refs() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "covered",
        "source_refs": ["S01"],
        "source_ids": [],
        "evidence_ids": ["evidence-1"],
        "orientation_summary": "The source establishes the mechanism.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.9,
        "next_action": "research",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")

    codes = {
        item["code"] for item in canonical_workspace_violations(workspace, require_syntheses=True)
    }

    assert "invalid_coverage_synthesis" in codes


def test_run_wide_synthesis_evidence_must_belong_to_its_declared_sources() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    workspace = _workspace(
        [first, second],
        source_sets=[
            {
                "set_id": "set_1",
                "source_refs": ["S01"],
                "question_ids": ["q_main"],
                "status": "mapped",
            },
            {
                "set_id": "set_2",
                "source_refs": ["S02"],
                "question_ids": ["q_main"],
                "status": "mapped",
            },
        ],
        outcomes=[
            {
                "outcome_id": "outcome:q_main:set_1",
                "set_id": "set_1",
                "question_id": "q_main",
                "status": "covered",
                "source_refs": ["S01"],
                "source_ids": ["source-1"],
                "evidence_ids": ["evidence-1"],
                "orientation_summary": "The first set covers its part.",
                "confidence": 0.9,
                "next_action": "research",
            },
            {
                "outcome_id": "outcome:q_main:set_2",
                "set_id": "set_2",
                "question_id": "q_main",
                "status": "covered",
                "source_refs": ["S02"],
                "source_ids": ["source-2"],
                "evidence_ids": ["evidence-2"],
                "orientation_summary": "The second set covers its part.",
                "confidence": 0.9,
                "next_action": "research",
            },
        ],
    )
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "covered",
        "source_refs": ["S01"],
        "source_ids": ["source-1"],
        "evidence_ids": ["evidence-2"],
        "orientation_summary": "The first source establishes the mechanism.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.9,
        "next_action": "research",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")

    violation = next(
        item
        for item in canonical_workspace_violations(workspace, require_syntheses=True)
        if item["code"] == "invalid_coverage_synthesis"
    )

    assert violation["evidence_ids"] == ["evidence-2"]


def test_multi_source_outcome_cannot_broaden_exact_assessment_evidence_binding() -> None:
    first = _assessment("S01", "source-1", ["evidence-1"])
    second = _assessment("S02", "source-2", ["evidence-2"])
    workspace = _workspace([first, second])
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "covered",
        "source_refs": ["S01"],
        "source_ids": ["source-1"],
        "evidence_ids": ["evidence-2"],
        "orientation_summary": "The first source establishes the mechanism.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.9,
        "next_action": "research",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")

    violation = next(
        item
        for item in canonical_workspace_violations(workspace, require_syntheses=True)
        if item["code"] == "invalid_coverage_synthesis"
    )

    assert violation["evidence_ids"] == ["evidence-2"]


def test_run_wide_synthesis_from_another_mapped_set_reaches_research_handoff() -> None:
    deferred = _assessment(
        "S01",
        "source-1",
        ["evidence-1"],
        representation_status="unreadable",
        disposition="deferred",
    )
    deferred["disposition_reason"] = "The checked representation is unreadable."
    active = _assessment("S02", "source-2", ["evidence-2"])
    deferred["set_id"] = "set_assigned"
    active["set_id"] = "set_other"
    source_sets = [
        {
            "set_id": "set_assigned",
            "label": "Assigned but unavailable",
            "purpose": "Check the originally assigned source.",
            "source_refs": ["S01"],
            "question_ids": ["q_main"],
            "status": "deferred",
            "deferred_reason": "The assigned source cannot answer the question.",
        },
        {
            "set_id": "set_other",
            "label": "Other mapped material",
            "purpose": "Map another part of the corpus.",
            "source_refs": ["S02"],
            "question_ids": ["q_main"],
            "status": "mapped",
            "findings_summary": "This set contains the answer.",
            "evidence_ids": ["evidence-2"],
        },
    ]
    outcomes = [
        {
            "outcome_id": "outcome:q_main:set_assigned",
            "set_id": "set_assigned",
            "question_id": "q_main",
            "status": "deferred",
            "source_ids": [],
            "evidence_ids": [],
            "deferred_reason": "The assigned source cannot answer the question.",
            "next_action": "none",
        },
        {
            "outcome_id": "outcome:q_main:set_other",
            "set_id": "set_other",
            "question_id": "q_main",
            "status": "covered",
            "source_refs": ["S02"],
            "source_ids": ["source-2"],
            "evidence_ids": ["evidence-2"],
            "orientation_summary": "The other mapped set establishes the answer.",
            "missing_detail": "",
            "conflict_summary": "",
            "deferred_reason": "",
            "next_action": "research",
        },
    ]
    workspace = _workspace([deferred, active], source_sets=source_sets, outcomes=outcomes)
    workspace["deferred_sources"] = [
        {"source_ref": "S01", "reason": "The assigned source cannot answer the question."}
    ]
    synthesis = {
        "outcome_id": "synthesis:q_main",
        "question_id": "q_main",
        "status": "covered",
        "source_refs": ["S02"],
        "source_ids": ["source-2"],
        "evidence_ids": ["evidence-2"],
        "orientation_summary": "Another mapped set establishes the answer.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
        "next_action": "research",
        "input_revision": 0,
    }
    workspace["coverage_syntheses"] = [synthesis]
    synthesis["input_hash"] = coverage_synthesis_input_hash(workspace, "q_main")

    assert canonical_workspace_violations(workspace, require_syntheses=True) == []
    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    audit = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-1", "source-2"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )

    assert [item.question_ids for item in brief.research_handoff] == [["q_main"]]
    assert brief.research_handoff[0].preferred_source_ids == ["source-2"]
    assert audit.passed is True


def test_canonical_workspace_rejects_active_outcome_for_deferred_source_and_set() -> None:
    assessment = _assessment(
        "S01",
        "source-1",
        ["evidence-1"],
        disposition="deferred",
    )
    workspace = _workspace([assessment])
    workspace["deferred_sources"] = [{"source_ref": "S01", "reason": "Explicitly deferred."}]
    workspace["source_sets"][0].update({"status": "deferred", "deferred_reason": "The set is deferred."})
    workspace["coverage_outcomes"][0]["source_refs"] = ["S01"]

    codes = {item["code"] for item in canonical_workspace_violations(workspace)}

    assert "deferred_source_active_outcome" in codes
    assert "deferred_set_active_outcome" in codes


def test_canonical_workspace_rejects_deferral_index_or_set_without_deferred_assessment() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    workspace["deferred_sources"] = [{"source_ref": "S01", "reason": "Contradictory index."}]
    workspace["source_sets"][0].update({"status": "deferred", "deferred_reason": "Contradictory set."})

    violations = canonical_workspace_violations(workspace)
    by_code = {item["code"]: item for item in violations}

    assert by_code["deferred_source_index_mismatch"]["source_refs"] == ["S01"]
    assert by_code["deferred_set_assessment_mismatch"]["source_refs"] == ["S01"]


def test_materialize_source_map_ranks_explicit_axes_then_corpus_order_never_evidence_count() -> None:
    many_evidence_supporting = _assessment(
        "S01",
        "source-many-evidence-supporting",
        [f"evidence-many-{index}" for index in range(20)],
        disposition="supporting",
    )
    uniqueness_lower = _assessment(
        "S02",
        "source-complementary",
        ["evidence-complementary"],
        uniqueness="complementary",
    )
    tied_first_in_corpus = _assessment(
        "S03",
        "source-unique-first",
        ["evidence-unique-first"],
    )
    authority_higher = _assessment(
        "S04",
        "source-primary",
        ["evidence-primary"],
        authority="primary",
        uniqueness="duplicate",
    )
    relevance_lower = _assessment(
        "S05",
        "source-supporting-relevance",
        ["evidence-supporting-relevance"],
        relevance="supporting",
        authority="primary",
    )
    tied_later_in_corpus = _assessment(
        "S06",
        "source-unique-later",
        [f"evidence-unique-later-{index}" for index in range(8)],
    )
    partial_background = _assessment(
        "S07",
        "source-partial-background",
        ["evidence-partial"],
        representation_status="partial",
        disposition="background",
    )
    workspace = _workspace(
        [
            many_evidence_supporting,
            uniqueness_lower,
            tied_first_in_corpus,
            authority_higher,
            relevance_lower,
            tied_later_in_corpus,
            partial_background,
        ]
    )

    source_map = _materialized_map(workspace)

    assert [item.source_id for item in source_map.source_triage.ranked_sources] == [
        "source-primary",
        "source-unique-first",
        "source-unique-later",
        "source-complementary",
        "source-supporting-relevance",
        "source-many-evidence-supporting",
        "source-partial-background",
    ]
    assert [item.rank for item in source_map.source_triage.ranked_sources] == list(range(1, 8))
    assert [item.score for item in source_map.source_triage.ranked_sources] == [
        4471.0,
        4464.0,
        4464.0,
        4463.0,
        4374.0,
        3464.0,
        2464.0,
    ]
    assert source_map.source_triage.top_sources_by_function["mechanism"] == [
        item.source_id for item in source_map.source_triage.ranked_sources
    ]
    assessments = {item.source_id: item for item in source_map.source_assessments}
    assert str(assessments["source-partial-background"].representation_status) == "partial"
    assert str(assessments["source-partial-background"].disposition) == "background"
    cards = {item.source_id: item for item in source_map.source_cards}
    assert str(cards["source-partial-background"].coverage_status) == "partial"
    assert str(cards["source-primary"].coverage_status) == "ready"


def test_materialize_corpus_brief_keeps_only_global_and_question_cautions_without_truncation() -> None:
    per_source_limitation = "Source-specific limitation remains in SourceMap."
    assessment = _assessment(
        "S01",
        "source-1",
        ["evidence-1"],
    )
    assessment["limitations"] = [per_source_limitation]
    workspace = _workspace([assessment])
    long_gap = "Global map gap: " + "g" * 2_000
    long_missing = "Question missing detail: " + "m" * 2_000
    long_conflict = "Question conflict detail: " + "c" * 2_000
    long_deferred = "Question deferred detail: " + "d" * 2_000
    workspace["coverage_outcomes"][0].update(
        {
            "status": "partial",
            "missing_detail": long_missing,
            "conflict_summary": long_conflict,
            "deferred_reason": long_deferred,
        }
    )
    source_map = _materialized_map(workspace)
    legacy_gap = "Legacy global map gap"
    source_map.gaps = [legacy_gap]
    source_map.source_triage.gaps = [long_gap]

    brief = _materialized_brief(workspace, source_map)

    assert brief.cautions == [legacy_gap, long_gap, long_missing, long_conflict, long_deferred]
    assert per_source_limitation not in brief.cautions
    assert source_map.source_assessments[0].limitations == [per_source_limitation]


def test_materialize_source_map_keeps_assessment_state() -> None:
    low_count = _assessment(
        "S01",
        "source-low-count",
        ["evidence-low"],
        representation_status="partial",
        disposition="background",
    )
    high_count = _assessment(
        "S02",
        "source-high-count",
        [f"evidence-high-{index}" for index in range(6)],
        representation_status="readable",
        disposition="anchor",
    )
    workspace = _workspace([low_count, high_count])

    source_map = _materialized_map(workspace)

    assessments = {item.source_id: item for item in source_map.source_assessments}
    assert str(assessments["source-low-count"].representation_status) == "partial"
    assert str(assessments["source-low-count"].disposition) == "background"
    assert str(assessments["source-high-count"].representation_status) == "readable"
    assert str(assessments["source-high-count"].disposition) == "anchor"
    cards = {item.source_id: item for item in source_map.source_cards}
    assert str(cards["source-low-count"].coverage_status) == "partial"
    assert str(cards["source-high-count"].coverage_status) == "ready"


def test_materialize_corpus_brief_excludes_deferred_and_unreadable_inputs_from_handoff() -> None:
    eligible = _assessment("S01", "source-eligible", ["evidence-eligible"])
    deferred = _assessment(
        "S02",
        "source-deferred",
        ["evidence-deferred"],
        disposition="deferred",
    )
    unreadable = _assessment(
        "S03",
        "source-unreadable",
        ["evidence-unreadable"],
        representation_status="unreadable",
    )
    workspace = _workspace([eligible, deferred, unreadable])
    source_map = _materialized_map(workspace)

    brief = _materialized_brief(workspace, source_map)

    assert len(brief.research_handoff) == 1
    handoff = brief.research_handoff[0]
    assert handoff.preferred_source_ids == ["source-eligible"]
    assert handoff.starting_evidence_ids == ["evidence-eligible"]


def test_corpus_brief_keeps_outcome_evidence_without_requiring_assessment_duplication() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-assessment"])
    workspace = _workspace([assessment])
    workspace["coverage_outcomes"][0]["evidence_ids"] = ["evidence-outcome"]
    card = _card("source-1", [])
    inventory = SourceMap(source_cards=[card])
    source_map = materialize_source_map_v2(
        SourceMap(source_cards=[card]),
        workspace=workspace,
        inventory=inventory,
    )

    brief = _materialized_brief(workspace, source_map)
    report = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-1"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )

    assert "evidence-outcome" in source_map.evidence_ids
    assert brief.research_handoff[0].starting_evidence_ids == ["evidence-outcome"]
    assert report.passed is True


def test_corpus_brief_excludes_three_observation_only_ids_from_fifty_eight_set_evidence_ids() -> None:
    selected_evidence_ids = [f"evidence-selected-{index:02d}" for index in range(55)]
    observation_only_ids = [f"evidence-observed-{index:02d}" for index in range(3)]
    assessment = _assessment("S01", "source-1", selected_evidence_ids)
    workspace = _workspace([assessment])
    workspace["source_sets"][0]["evidence_ids"] = [
        *selected_evidence_ids,
        *observation_only_ids,
    ]
    workspace["source_sets"][0]["observations"] = [
        {
            "source_refs": ["S01"],
            "evidence_ids": observation_only_ids,
            "note": "Read during triage but never promoted into a final record.",
        }
    ]

    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    report = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-1"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )

    assert len(workspace["source_sets"][0]["evidence_ids"]) == 58
    assert len(source_map.evidence_ids) == 55
    assert brief.source_sets[0].evidence_ids == selected_evidence_ids
    assert not set(observation_only_ids) & set(brief.source_sets[0].evidence_ids)
    assert report.passed is True
    assert report.checked_evidence_count == 55


def test_materialize_corpus_brief_never_activates_a_deferred_set() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    workspace["source_sets"][0].update({"status": "deferred", "deferred_reason": "Do not use this set in research."})
    source_map = _materialized_map(workspace)

    brief = _materialized_brief(workspace, source_map)

    assert brief.research_handoff == []


def test_backend_audit_blocks_source_from_deferred_set_even_if_assessment_is_active() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    workspace["source_sets"][0].update({"status": "deferred", "deferred_reason": "Do not use this set in research."})
    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    brief.research_handoff.append(
        ResearchHandoffItem(
            handoff_id="handoff:invalid-deferred-set",
            objective="Invalid deferred source",
            question_ids=["q_main"],
            preferred_source_ids=["source-1"],
            starting_evidence_ids=["evidence-1"],
        )
    )

    report = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-1"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )

    issue = next(item for item in report.issues if str(item.code) == "deferred_source_in_handoff")
    assert report.passed is False
    assert issue.source_ids == ["source-1"]


def test_materialize_corpus_brief_keeps_research_question_when_no_source_is_eligible() -> None:
    assessment = _assessment(
        "S01",
        "source-out-of-scope",
        ["evidence-1"],
        relevance="out_of_scope",
    )
    workspace = _workspace([assessment])
    workspace["coverage_outcomes"][0].update(
        {
            "status": "missing",
            "source_ids": [],
            "evidence_ids": [],
            "orientation_summary": "",
            "missing_detail": "The selected source does not answer the question.",
            "next_action": "research",
        }
    )
    source_map = _materialized_map(workspace)

    brief = _materialized_brief(workspace, source_map)

    assert len(brief.research_handoff) == 1
    assert brief.research_handoff[0].question_ids == ["q_main"]
    assert brief.research_handoff[0].preferred_source_ids == []
    assert brief.research_handoff[0].starting_evidence_ids == []


def test_backend_corpus_brief_audit_blocks_invalid_source_and_evidence_references() -> None:
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment])
    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    brief.source_sets[0].source_ids.append("source-outside-run")
    brief.coverage_outcomes[0].evidence_ids.append("evidence-outside-run")

    report = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-1"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )

    invalid = next(item for item in report.issues if str(item.code) == "invalid_reference")
    assert report.passed is False
    assert invalid.source_ids == ["source-outside-run"]
    assert invalid.evidence_ids == ["evidence-outside-run"]


def test_backend_audit_blocks_in_scope_evidence_from_deferred_handoff_source() -> None:
    eligible = _assessment("S01", "source-eligible", ["evidence-eligible"])
    deferred = _assessment(
        "S02",
        "source-deferred",
        ["evidence-deferred"],
        disposition="deferred",
    )
    workspace = _workspace([eligible, deferred])
    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    brief.research_handoff[0].starting_evidence_ids.append("evidence-deferred")

    report = audit_corpus_brief(
        corpus_brief_artifact_id=str(uuid.uuid4()),
        brief=brief,
        source_map=source_map,
        selected_source_ids={"source-eligible", "source-deferred"},
        expected_generation_run_id=brief.generation_run_id,
        expected_project_id=brief.project_id,
        expected_run_corpus_view_id=brief.run_corpus_view_id,
        expected_corpus_revision=brief.corpus_revision,
        expected_attempt=brief.attempt,
        expected_source_map_artifact_id=brief.source_map_artifact_id,
    )

    issue = next(item for item in report.issues if str(item.code) == "deferred_source_in_handoff" and item.evidence_ids)
    assert report.passed is False
    assert issue.evidence_ids == ["evidence-deferred"]


def test_capsule_cache_key_ignores_self_updated_timestamp() -> None:
    version = SimpleNamespace(
        id=uuid.uuid4(),
        content_hash="content",
        kind="extracted_text",
        storage_path="source.txt",
        provider_file_id="file-1",
        updated_at="first",
    )
    first = capsule_cache_key(version)  # type: ignore[arg-type]
    version.updated_at = "changed-by-capsule-cache-write"

    assert capsule_cache_key(version) == first  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_research_workspace_seeds_jobs_from_corpus_brief_and_persists_full_plus_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_question = "How does the institutional mechanism work? " * 30
    assessment = _assessment("S01", "source-1", ["evidence-1"])
    workspace = _workspace([assessment], question_text=long_question)
    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    captured: dict = {}

    async def load_workspace(_db, _run_id):
        return None

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        captured.update(workspace)
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(research_workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(research_workspace_module, "save_research_workspace", save_workspace)

    artifact_id = str(uuid.uuid4())
    created = await research_workspace_module.ensure_research_workspace(
        SimpleNamespace(info={}),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        source_map=source_map,
        source_inventory=source_map,
        run_view=SimpleNamespace(revision=2, attempt=3),
        corpus_brief=brief,
        corpus_brief_artifact_id=artifact_id,
    )

    assert created == captured
    assert created["corpus_brief_artifact_id"] == artifact_id
    assert created["corpus_brief"] == brief.model_dump(mode="json")
    assert created["corpus_brief"]["coverage_questions"][0]["question"] == long_question
    question_preview = created["corpus_brief_preview"]["coverage_questions"][0]["question"]
    assert question_preview["preview"] == long_question[:400]
    assert question_preview["chars"] == len(long_question)
    assert question_preview["truncated"] is True
    assert created["research_questions"] == [long_question]
    assert len(created["investigation_jobs"]) == 1
    job = created["investigation_jobs"][0]
    assert job["corpus_handoff_id"] == "handoff:q_main"
    assert job["coverage_question_ids"] == ["q_main"]
    assert job["source_refs"] == ["S01"]
    assert job["questions"] == [long_question]
    assert job["starting_evidence_ids"] == ["evidence-1"]
    assert job["evidence_ids"] == []


@pytest.mark.asyncio
async def test_research_workspace_excludes_sources_rejected_by_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligible = _assessment("S01", "source-eligible", ["evidence-eligible"])
    deferred = _assessment(
        "S02",
        "source-deferred",
        ["evidence-deferred"],
        disposition="deferred",
    )
    workspace = _workspace([eligible, deferred])
    source_map = _materialized_map(workspace)
    brief = _materialized_brief(workspace, source_map)
    captured: dict = {}

    async def load_workspace(_db, _run_id):
        return None

    async def save_workspace(_db, *, generation_run_id, workspace, status=None):
        del generation_run_id, status
        captured.update(workspace)
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(research_workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(research_workspace_module, "save_research_workspace", save_workspace)

    created = await research_workspace_module.ensure_research_workspace(
        SimpleNamespace(info={}),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        source_map=source_map,
        source_inventory=source_map,
        run_view=SimpleNamespace(revision=1, attempt=1),
        corpus_brief=brief,
        corpus_brief_artifact_id=str(uuid.uuid4()),
    )

    assert created["source_order"] == ["S01"]
    assert all("S02" not in job["source_refs"] for job in created["investigation_jobs"])
