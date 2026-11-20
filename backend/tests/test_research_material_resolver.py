import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.material import report as parse_report
from podgen.research.materials.loading import load_materials
from podgen.research.materials.preparation import prepare_complete_materials
from podgen.research.materials.resolver import ResearchMaterialResolver
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.sources.acquisition import service
from tests.source_acquisition_support import ARTICLE, Fetcher, document, setup


def save_report(journal, text):
    material = parse_report(dict(steps=[dict(type="model_output", content=[dict(type="text", text=text)])]))
    material.pop("text")
    journal.put(
        "result",
        dict(
            status="completed",
            report_text_blob=journal.blob(text.encode(), "text/markdown"),
            material=material,
        ),
    )


def fixture(repo, scope, producer, monkeypatch):
    monkeypatch.setattr(service, "_check_ssrf", lambda url: None)
    project, _ = scope
    request, _ = setup(repo, scope, producer)
    report = "# Report\n\nOriginal explanation. [cite: 11]\n\n**Sources:**\n11. [Document](https://example.com/article)\n"
    journal = ResearchJournal(repo, project, uuid4())
    journal.put(
        "manifest",
        dict(
            config=dict(
                assignment=dict(
                    user_request="Question", output_language="ru", scope=dict(source_mode="open_web")
                ),
                instructions="Research the requested question.",
                profile=dict(agent="deep-research-preview-04-2026"),
            )
        ),
    )
    save_report(journal, report)
    root = Path(__file__).resolve().parents[2]
    assignment = json.loads((root / "evals/editorial-inputs/v1/snow-pump/shared.json").read_text())[
        "assignment"
    ]
    config = dict(
        operation_id=str(uuid4()),
        research_run_id=str(journal.run_id),
        assignment=assignment,
        acquisition=dict(
            profile_ref=request.profile_ref.model_dump(mode="json"),
            rights=request.rights.model_dump(mode="json"),
        ),
    )
    fetcher = Fetcher(
        {
            "https://example.com/article": document(),
            "https://example.com/new": document("Ёжик и точные часы. " * 100, url="https://example.com/new"),
        }
    )

    async def acquire(repo, project, request):
        return await service.acquire_source(repo, project, request, fetcher=fetcher)

    shared = asyncio.run(prepare_complete_materials(repo, project, config, acquire=acquire))
    materials = load_materials(repo, project, shared, case_id=assignment["key"])
    return materials, config, acquire, fetcher


def test_complete_preparation_replay_and_runtime_source_survive_new_resolver(
    repo, scope, producer, monkeypatch
):
    materials, config, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    assert len(fetcher.calls) == 1
    assert (
        asyncio.run(prepare_complete_materials(repo, scope[0], config, acquire=acquire))
        == materials.shared_ref
    )
    assert len(fetcher.calls) == 1
    journal = EvaluationJournal(repo, scope[0])
    resolver = ResearchMaterialResolver(journal, materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    result = resolver.resolve(
        "request_source",
        dict(url="https://example.com/new", reason="Verify the observed date."),
        operation_id=operation,
        run_id=run_id,
        guard=lambda: None,
    )
    assert result.result["status"] == "resolved"
    assert result.result["source_id"] == "S12"
    assert result.admissions[0].text[:] == "Ёжик и точные часы. " * 100
    assert len(fetcher.calls) == 2
    restored = ResearchMaterialResolver(journal, materials, acquire=acquire)
    assert restored.restore(run_id=run_id, guard=lambda: None) == result.admissions
    again = restored.resolve(
        "request_source",
        dict(url="https://example.com/new#section", reason="Read the exact passage."),
        operation_id=uuid4(),
        run_id=run_id,
        guard=lambda: None,
    )
    assert again.result["source_id"] == "S12"
    assert len(fetcher.calls) == 2
    assert len(materials.packet.sources) == 1
    assert materials.documents[1].text[:] == ARTICLE


def test_specific_research_handoff_resumes_same_question_and_separates_report_from_evidence(
    repo, scope, producer, monkeypatch
):
    materials, config, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(question="When was the second trial?", reason="The dated passage is missing.")
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert pending.result["status"] == "pending"
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    assert child.get("approval") is None
    assert child.get("manifest")["config"]["assignment"]["user_request"] == args["question"]
    assert "parent_report_ref" not in child.get("manifest")["config"]["assignment"]
    assert "record_kind" not in child.get("manifest")["request"]["body"]["input"]
    assert (
        resolver.resolve("request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None)
        == pending
    )
    save_report(
        child,
        "# Targeted result\n\nThe trial happened in May. [cite: 1]\n\n**Sources:**\n1. [Date](https://example.com/new)\n",
    )
    done = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert done.result["status"] == "resolved"
    assert done.result["report_id"] == "X1"
    assert [a.source_id for a in done.admissions] == ["S12", "X1"]
    assert isinstance(done.admissions[1].reference, RuntimeRef)
    assert done.admissions[1].catalog["reported_source_map"][0]["source_id"] == "S12"
    assert (
        resolver.resolve("request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None)
        == done
    )
    assert len(fetcher.calls) == 2


def test_restoration_checks_original_input_binding(repo, scope, producer, monkeypatch):
    materials, _, acquire, _ = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id = uuid4()
    resolver.resolve(
        "request_source",
        dict(url="https://example.com/new", reason="Read"),
        operation_id=uuid4(),
        run_id=run_id,
        guard=lambda: None,
    )
    from dataclasses import replace

    resolver.materials = replace(
        materials, shared_ref=RuntimeRef(record_kind="user_input", record_id=uuid4())
    )
    with pytest.raises(ContractError, match="input_mismatch"):
        resolver.restore(run_id=run_id, guard=lambda: None)


@pytest.mark.parametrize("parent_kind", ["fixed", "continuation_before_manifest"])
def test_reused_plan_freezes_original_admissions_and_reserves_ids(
    repo, scope, producer, monkeypatch, parent_kind
):
    from dataclasses import replace
    from types import SimpleNamespace

    from podgen.research.materials.admissions import admit_report

    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    journal = EvaluationJournal(repo, scope[0])
    resolver = ResearchMaterialResolver(journal, materials, acquire=acquire)
    parent_id, child_id = uuid4(), uuid4()
    first = resolver.resolve(
        "request_source",
        dict(url="https://example.com/new", reason="Read"),
        operation_id=uuid4(),
        run_id=parent_id,
        guard=lambda: None,
    ).admissions[0]
    report = ResearchJournal(repo, scope[0], uuid4())
    save_report(report, "# Supplemental\n\nAn exact retained result.")
    original_report = admit_report(resolver.store(parent_id), materials, report, "Question", [])
    reused = ResearchMaterialResolver(
        journal,
        replace(materials, fixed=SimpleNamespace(source_run_id=parent_id))
        if parent_kind == "fixed"
        else materials,
        acquire=acquire,
        parent_run_ids=() if parent_kind == "fixed" else (parent_id,),
    )
    inherited = reused.restore(run_id=child_id, guard=lambda: None)
    assert inherited == (first, original_report)
    assert inherited[0].catalog["admission_ref"] == first.catalog["admission_ref"]
    later = ResearchJournal(repo, scope[0], uuid4())
    save_report(later, "# Later\n\nThis appeared after child admission.")
    admit_report(resolver.store(parent_id), materials, later, "Later", [])
    assert reused.restore(run_id=child_id, guard=lambda: None) == inherited
    own_report = admit_report(reused.store(child_id), materials, later, "Child question", [])
    assert own_report.source_id == "X2"
    fetcher.documents["https://example.com/third"] = document(
        "Third source " * 100, url="https://example.com/third"
    )
    added = reused.resolve(
        "request_source",
        dict(url="https://example.com/third", reason="Read"),
        operation_id=uuid4(),
        run_id=child_id,
        guard=lambda: None,
    )
    assert added.result["source_id"] == "S13"
    assert [row.source_id for row in reused.restore(run_id=child_id, guard=lambda: None)] == [
        "S12",
        "X1",
        "X2",
        "S13",
    ]
    assert len(fetcher.calls) == 3


def test_anchored_targeted_research_uses_special_task_and_validates_retained_excerpt(
    repo, scope, producer, monkeypatch
):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(
        question="Верен ли смысл этой реплики?",
        reason="Уточнить дату.",
        anchors=[],
        research_contract="targeted-script-context/1",
        script_context=dict(
            script_ref={"technical": "private"},
            script_hash="bound-hash",
            anchors=[dict(utterance_index=1, quote="точные часы")],
            utterances=[dict(utterance_index=1, speaker="A", text="Ёжик и точные часы.")],
        ),
    )
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert pending.result["contract"] == "targeted-research-handoff/2"
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    manifest = child.get("manifest")
    request = manifest["request"]["body"]
    serialized = json.dumps(request, ensure_ascii=False)
    assert "Ёжик и точные часы." in serialized
    assert "episode_assignment" not in serialized and "private" not in serialized
    assert "Do not fabricate" in manifest["config"]["instructions"]
    save_report(
        child,
        json.dumps(
            dict(
                answer="Точная выдержка получена.",
                limitations="",
                evidence=[
                    dict(
                        url="https://example.com/new", quote="Ёжик и точные часы.", relation="Исходные слова."
                    )
                ],
            ),
            ensure_ascii=False,
        ),
    )
    done = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert done.result["excerpts"][0] == dict(
        source_id="S12", quote="Ёжик и точные часы.", start=0, end=19, relation="Исходные слова."
    )
    assert done.result["report_id"] == "X1"
    assert done.result["relationship"] == "validated_source_locations_not_read_receipts"
    assert len(fetcher.calls) == 2
    assert (
        resolver.resolve("request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None)
        == done
    )
    assert len(fetcher.calls) == 2


@pytest.mark.parametrize("available", [False, True])
def test_targeted_evidence_uses_source_path_and_never_admits_unavailable_answer(
    repo, scope, producer, monkeypatch, available
):
    from dataclasses import replace

    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    materials = replace(materials, documents=tuple(reversed(materials.documents)))
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(
        question="Read the exact record.",
        reason="Verify",
        anchors=[],
        research_contract="targeted-script-context/1",
        script_context=None,
    )
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    url = "https://example.com/article" if available else "https://example.com/missing"
    if not available:
        fetcher.documents[url] = document("Not found", url=url, status=404)
    save_report(
        child,
        json.dumps(
            dict(
                answer="The original account.",
                limitations="",
                evidence=[dict(url=url, quote=ARTICLE[:80], relation="The record.")],
            ),
            ensure_ascii=False,
        ),
    )
    result = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    if available:
        assert result.result["excerpts"][0]["source_id"] == "S11"
        assert result.result["excerpts"][0]["quote"] == ARTICLE[:80]
        assert len(fetcher.calls) == 1
    else:
        assert result.result["status"] == "unavailable"
        assert result.result["error"] == "targeted_research_evidence_unavailable"
        assert not any(
            a.source_id.startswith("X") for a in resolver.restore(run_id=run_id, guard=lambda: None)
        )


def test_narrow_research_parallel_resolution_and_publication_retry_reuse_acquisition(
    repo, scope, producer, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(
        question="Inspect exact words.",
        reason="Verify",
        anchors=[],
        research_contract="targeted-script-context/1",
        script_context=None,
    )
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    save_report(
        child,
        json.dumps(
            dict(
                answer="The words.",
                limitations="",
                evidence=[
                    dict(url="https://example.com/new", quote="Ёжик и точные часы.", relation="The record.")
                ],
            ),
            ensure_ascii=False,
        ),
    )
    from podgen.research.materials import targeted

    compact = targeted.compact
    attempts = []

    def interrupted(*args):
        attempts.append(True)
        raise RuntimeError("Interrupted after retained acquisition")

    monkeypatch.setattr(targeted, "compact", interrupted)
    with pytest.raises(RuntimeError, match="retained acquisition"):
        resolver.resolve("request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None)
    assert len(fetcher.calls) == 2
    monkeypatch.setattr(targeted, "compact", compact)

    def resolve(_):
        from podgen.domain.errors import Conflict

        try:
            return ResearchMaterialResolver(
                EvaluationJournal(repo, scope[0]), materials, acquire=acquire
            ).resolve("request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None)
        except Conflict as error:
            assert str(error) == "hosted_research_run_busy"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, range(2)))
    first = next(value for value in results if value is not None)
    second = resolve(0)
    assert first == second and first.result["status"] == "resolved"
    assert len(fetcher.calls) == 2
    assert len([a for a in first.admissions if a.source_id.startswith("X")]) == 1


def test_targeted_limitations_without_evidence_are_unresolved(repo, scope, producer, monkeypatch):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(
        question="Check the record.",
        reason="Verify",
        anchors=[],
        research_contract="targeted-script-context/1",
        script_context=None,
    )
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    save_report(
        child,
        json.dumps(
            dict(
                answer="No answer established.",
                limitations="No original document could be inspected.",
                evidence=[],
            )
        ),
    )
    result = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert result.result["status"] == "unresolved"
    assert not any(a.source_id.startswith("X") for a in resolver.restore(run_id=run_id, guard=lambda: None))
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize("malformed", [False, True])
def test_invalid_targeted_answer_returns_explicit_unresolved_result_without_repeating_work(
    repo, scope, producer, monkeypatch, malformed
):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(
        question="Verify this date.",
        reason="Check",
        research_contract="targeted-script-context/1",
        script_context=None,
    )
    pending = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    save_report(
        child,
        "malformed saved answer"
        if malformed
        else json.dumps(
            dict(
                answer="A claim.",
                limitations="",
                evidence=[
                    dict(url="https://example.com/new", quote="Invented missing words.", relation="The date.")
                ],
            )
        ),
    )
    result = resolver.resolve(
        "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
    )
    assert result.result["status"] == "unresolved"
    assert result.result["error"] == (
        "targeted_research_invalid_answer" if malformed else "targeted_research_quote_not_in_source"
    )
    assert not any(a.source_id.startswith("X") for a in resolver.restore(run_id=run_id, guard=lambda: None))
    calls = len(fetcher.calls)
    assert (
        resolver.resolve("request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None)
        == result
    )
    assert len(fetcher.calls) == calls
