from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.contracts.pipeline import V2Phase, V2PipelineStep
from app.generation_v2.corpus_scope import session_corpus_scope_or_none
from app.generation_v2.pipeline.status import V2StepExecutionStopped
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion, SourceTriageGate
from app.generation_v2.source_corpus import load_current_v2_corpus
from app.generation_v2.source_content_readiness import CONTENT_ASSESSMENT_SCHEMA_VERSION
from app.models.generation_v2 import (
    SourceRepresentationKind,
    SourceVersion,
    V2ArtifactStatus,
    V2ArtifactType,
    V2RecordStatus,
)
from app.models.source import Source, SourceType


class _ReachedEditorial(RuntimeError):
    pass


def _run(*, stop_after: str | None = None) -> SimpleNamespace:
    config = {}
    if stop_after is not None:
        config = {"execution_mode": "step", "v2_stop_after_step": stop_after}
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        show_bible_id=None,
        config=config,
        current_phase=None,
        current_step=None,
        progress_pct=None,
    )


def _passed_triage(corpus: object) -> SimpleNamespace:
    return SimpleNamespace(
        corpus=corpus,
        triage_completion=SourceTriageCompletion(ready=True, summary="ready"),
        triage_gate=SourceTriageGate(passed=True, summary="passed"),
    )


def _patch_orchestrator_basics(monkeypatch: pytest.MonkeyPatch, module, calls: list[str]) -> object:
    runtime = object()
    show_bible = object()

    async def load_show_bible(_db, _run):
        return show_bible

    async def acquisition_rounds(_db, _run_id):
        return 0

    async def acquisition_must_not_run(*_args, **_kwargs):
        raise AssertionError("source acquisition must not run for passed triage")

    monkeypatch.setattr(module, "V2Runtime", lambda: runtime)
    monkeypatch.setattr(module, "_load_show_bible_context", load_show_bible)
    monkeypatch.setattr(module, "persisted_source_acquisition_rounds_used", acquisition_rounds)
    monkeypatch.setattr(module, "_run_source_acquisition_until_triage_ready", acquisition_must_not_run)
    return SimpleNamespace(runtime=runtime, show_bible=show_bible, calls=calls)


@pytest.mark.asyncio
async def test_source_triage_step_reuses_prepared_corpus_without_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run(stop_after=V2PipelineStep.SOURCE_TRIAGE)
    db = object()
    corpus = SimpleNamespace(run_view=object())

    async def prepare_must_not_run(*_args, **_kwargs):
        raise AssertionError("source_triage must not prepare a new corpus attempt")

    async def load_current(actual_db, actual_run):
        assert (actual_db, actual_run) == (db, run)
        calls.append("load_current_corpus")
        return corpus

    async def load_current_view(actual_db, run_id):
        assert (actual_db, run_id) == (db, run.id)
        calls.append("load_current_view")
        return corpus.run_view

    async def has_history(actual_db, run_view):
        assert (actual_db, run_view) == (db, corpus.run_view)
        calls.append("detect_partial_triage")
        return False

    async def triage(actual_run, actual_db, actual_runtime, *, corpus: object):
        assert (actual_run, actual_db, actual_runtime) == (run, db, deps.runtime)
        assert corpus is globals_corpus
        calls.append("triage")
        return _passed_triage(corpus)

    async def commit(_run, _db, step, _pct, *, next_step):
        calls.append(f"commit:{step}")
        raise V2StepExecutionStopped(str(step), str(next_step))

    async def research_must_not_run(*_args, **_kwargs):
        raise AssertionError("step execution must stop after source_triage")

    globals_corpus = corpus
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", prepare_must_not_run)
    monkeypatch.setattr(orchestrator, "load_current_run_corpus_view", load_current_view)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_current)
    monkeypatch.setattr(orchestrator, "current_attempt_has_triage_history", has_history)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", triage)
    monkeypatch.setattr(orchestrator, "commit_step", commit)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", research_must_not_run)

    with pytest.raises(V2StepExecutionStopped) as exc_info:
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert exc_info.value.executed_step == V2PipelineStep.SOURCE_TRIAGE
    assert exc_info.value.next_step == V2PipelineStep.EDITORIAL_RESEARCH
    assert calls == [
        "load_current_view",
        "detect_partial_triage",
        "load_current_corpus",
        "triage",
        "commit:source_triage",
    ]


@pytest.mark.asyncio
async def test_legacy_source_triage_retry_starts_a_clean_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run(stop_after=V2PipelineStep.SOURCE_TRIAGE)
    db = object()
    old_view = SimpleNamespace(revision=1, attempt=1, selected_source_ids=[])
    fresh_corpus = SimpleNamespace(run_view=object())

    async def load_current_view(_db, run_id):
        assert run_id == run.id
        calls.append("load_current_view")
        return old_view

    async def load_current_must_not_run(*_args, **_kwargs):
        raise AssertionError("retry history must be checked before hydrating rejected pinned versions")

    async def has_history(_db, run_view):
        assert run_view is old_view
        calls.append("detect_partial_triage")
        return True

    async def load_legacy_workspace(_db, run_id):
        assert run_id == run.id
        calls.append("load_legacy_workspace")
        return {
            "schema_version": "source_triage_workspace.v2",
            "corpus_revision": 1,
            "attempt": 1,
            "status": "in_progress",
        }

    async def prepare(_db, _run):
        calls.append("prepare_fresh_attempt")
        return fresh_corpus

    async def triage(_run, _db, actual_runtime, *, corpus):
        assert actual_runtime is deps.runtime
        assert corpus is fresh_corpus
        calls.append("triage")
        return _passed_triage(corpus)

    async def commit(_run, _db, step, _pct, *, next_step):
        calls.append(f"commit:{step}")
        raise V2StepExecutionStopped(str(step), str(next_step))

    async def research_must_not_run(*_args, **_kwargs):
        raise AssertionError("step execution must stop after source_triage")

    monkeypatch.setattr(orchestrator, "load_current_run_corpus_view", load_current_view)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_current_must_not_run)
    monkeypatch.setattr(orchestrator, "current_attempt_has_triage_history", has_history)
    monkeypatch.setattr(orchestrator, "load_source_triage_workspace", load_legacy_workspace)
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", prepare)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", triage)
    monkeypatch.setattr(orchestrator, "commit_step", commit)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", research_must_not_run)

    with pytest.raises(V2StepExecutionStopped):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == [
        "load_current_view",
        "detect_partial_triage",
        "load_legacy_workspace",
        "prepare_fresh_attempt",
        "triage",
        "commit:source_triage",
    ]


@pytest.mark.asyncio
async def test_source_triage_refreshes_corpus_when_explicit_selection_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run(stop_after=V2PipelineStep.SOURCE_TRIAGE)
    run.config["source_ids"] = [str(uuid.uuid4())]
    db = object()
    old_view = SimpleNamespace(selected_source_ids=[str(uuid.uuid4())])
    fresh_corpus = SimpleNamespace(run_view=object())

    async def load_current_view(_db, run_id):
        assert run_id == run.id
        calls.append("load_current_view")
        return old_view

    async def history_must_not_run(*_args, **_kwargs):
        raise AssertionError("Changed explicit selection must short-circuit history loading")

    async def prepare(_db, _run):
        calls.append("prepare_fresh_revision")
        return fresh_corpus

    async def triage(_run, _db, actual_runtime, *, corpus):
        assert actual_runtime is deps.runtime
        assert corpus is fresh_corpus
        calls.append("triage")
        return _passed_triage(corpus)

    async def commit(_run, _db, step, _pct, *, next_step):
        calls.append(f"commit:{step}")
        raise V2StepExecutionStopped(str(step), str(next_step))

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("stale explicit selection must not be hydrated or researched")

    monkeypatch.setattr(orchestrator, "load_current_run_corpus_view", load_current_view)
    monkeypatch.setattr(
        orchestrator,
        "current_attempt_has_triage_history",
        history_must_not_run,
    )
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", must_not_run)
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", prepare)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", triage)
    monkeypatch.setattr(orchestrator, "commit_step", commit)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", must_not_run)

    with pytest.raises(V2StepExecutionStopped):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == [
        "load_current_view",
        "prepare_fresh_revision",
        "triage",
        "commit:source_triage",
    ]


@pytest.mark.asyncio
async def test_editorial_research_step_hydrates_passed_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run(stop_after=V2PipelineStep.EDITORIAL_RESEARCH)
    db = object()
    corpus = object()
    triage_state = _passed_triage(corpus)

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("earlier stage must not be rerun for editorial_research")

    async def load_current(_db, _run):
        calls.append("load_current_corpus")
        return corpus

    async def load_triage(actual_db, actual_run):
        assert (actual_db, actual_run) == (db, run)
        calls.append("load_passed_triage")
        return triage_state

    async def enter_research(
        actual_db,
        actual_run,
        actual_runtime,
        *,
        triage_state: object,
        acquisition_rounds_used: int,
        show_bible: object,
    ):
        assert (actual_db, actual_run, actual_runtime) == (db, run, deps.runtime)
        assert triage_state is globals_triage_state
        assert acquisition_rounds_used == 0
        assert show_bible is deps.show_bible
        calls.append("editorial_research")
        raise _ReachedEditorial

    globals_triage_state = triage_state
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", must_not_run)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", must_not_run)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_current)
    monkeypatch.setattr(orchestrator, "load_passed_triage_state", load_triage)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", enter_research)

    with pytest.raises(_ReachedEditorial):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == ["load_current_corpus", "load_passed_triage", "editorial_research"]


@pytest.mark.asyncio
async def test_full_mode_runs_prepare_then_triage_on_same_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run()
    db = object()
    corpus = object()
    triage_state = _passed_triage(corpus)

    async def prepare(actual_db, actual_run):
        assert (actual_db, actual_run) == (db, run)
        calls.append("prepare")
        return corpus

    async def no_current_view(actual_db, run_id):
        assert (actual_db, run_id) == (db, run.id)
        calls.append("load_current_view")
        return None

    async def load_must_not_run(*_args, **_kwargs):
        raise AssertionError("full mode must use the corpus it just prepared")

    async def triage(actual_run, actual_db, actual_runtime, *, corpus: object):
        assert (actual_run, actual_db, actual_runtime) == (run, db, deps.runtime)
        assert corpus is globals_corpus
        calls.append("triage")
        return triage_state

    async def commit(_run, _db, step, _pct, *, next_step):
        calls.append(f"commit:{step}->{next_step}")

    async def enter_research(_db, _run, _runtime, *, triage_state: object, **_kwargs):
        assert triage_state is globals_triage_state
        calls.append("editorial_research")
        raise _ReachedEditorial

    globals_corpus = corpus
    globals_triage_state = triage_state
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", prepare)
    monkeypatch.setattr(orchestrator, "load_current_run_corpus_view", no_current_view)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_must_not_run)
    monkeypatch.setattr(orchestrator, "load_passed_triage_state", load_must_not_run)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", triage)
    monkeypatch.setattr(orchestrator, "commit_step", commit)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", enter_research)

    with pytest.raises(_ReachedEditorial):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == [
        "load_current_view",
        "prepare",
        "commit:corpus_prepare->source_triage",
        "triage",
        "commit:source_triage->editorial_research",
        "editorial_research",
    ]


@pytest.mark.asyncio
async def test_full_mode_resumes_current_v3_triage_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run()
    db = object()
    current_view = SimpleNamespace(
        revision=2,
        attempt=3,
        selected_source_ids=[],
    )
    corpus = SimpleNamespace(run_view=current_view)
    triage_state = _passed_triage(corpus)

    async def current_view_for_run(actual_db, run_id):
        assert (actual_db, run_id) == (db, run.id)
        calls.append("load_current_view")
        return current_view

    async def has_triage_history(actual_db, actual_view):
        assert (actual_db, actual_view) == (db, current_view)
        calls.append("detect_triage_history")
        return True

    async def load_workspace(actual_db, run_id):
        assert (actual_db, run_id) == (db, run.id)
        calls.append("load_v3_workspace")
        return {
            "schema_version": "source_triage_workspace.v3",
            "corpus_revision": 2,
            "attempt": 3,
            "status": "in_progress",
            "current_set": {"set_id": "set_1"},
        }

    async def load_current(actual_db, actual_run):
        assert (actual_db, actual_run) == (db, run)
        calls.append("load_current_corpus")
        return corpus

    async def prepare_must_not_run(*_args, **_kwargs):
        raise AssertionError("A current V3 set must resume without a new corpus attempt")

    async def triage(actual_run, actual_db, actual_runtime, *, corpus: object):
        assert (actual_run, actual_db, actual_runtime) == (run, db, deps.runtime)
        assert corpus is globals_corpus
        calls.append("triage")
        return triage_state

    async def commit(_run, _db, step, _pct, *, next_step):
        calls.append(f"commit:{step}->{next_step}")

    async def enter_research(_db, _run, _runtime, *, triage_state: object, **_kwargs):
        assert triage_state is globals_triage_state
        calls.append("editorial_research")
        raise _ReachedEditorial

    globals_corpus = corpus
    globals_triage_state = triage_state
    monkeypatch.setattr(orchestrator, "load_current_run_corpus_view", current_view_for_run)
    monkeypatch.setattr(orchestrator, "current_attempt_has_triage_history", has_triage_history)
    monkeypatch.setattr(orchestrator, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_current)
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", prepare_must_not_run)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", triage)
    monkeypatch.setattr(orchestrator, "commit_step", commit)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", enter_research)

    with pytest.raises(_ReachedEditorial):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == [
        "load_current_view",
        "detect_triage_history",
        "load_v3_workspace",
        "load_current_corpus",
        "commit:corpus_prepare->source_triage",
        "triage",
        "commit:source_triage->editorial_research",
        "editorial_research",
    ]


@pytest.mark.asyncio
async def test_full_mode_restarts_legacy_v2_triage_in_a_fresh_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import orchestrator

    calls: list[str] = []
    deps = _patch_orchestrator_basics(monkeypatch, orchestrator, calls)
    run = _run()
    db = object()
    legacy_view = SimpleNamespace(
        revision=1,
        attempt=1,
        selected_source_ids=[],
    )
    fresh_corpus = SimpleNamespace(run_view=SimpleNamespace(revision=1, attempt=2))
    triage_state = _passed_triage(fresh_corpus)

    async def current_view_for_run(_db, _run_id):
        calls.append("load_current_view")
        return legacy_view

    async def has_triage_history(_db, _run_view):
        calls.append("detect_triage_history")
        return True

    async def load_workspace(_db, _run_id):
        calls.append("load_legacy_workspace")
        return {
            "schema_version": "source_triage_workspace.v2",
            "corpus_revision": 1,
            "attempt": 1,
            "status": "in_progress",
        }

    async def prepare(_db, _run):
        calls.append("prepare_fresh_attempt")
        return fresh_corpus

    async def load_current_must_not_run(*_args, **_kwargs):
        raise AssertionError("Legacy V2 triage must not reuse its contaminated attempt")

    async def triage(_run, _db, actual_runtime, *, corpus):
        assert actual_runtime is deps.runtime
        assert corpus is fresh_corpus
        calls.append("triage")
        return triage_state

    async def commit(_run, _db, step, _pct, *, next_step):
        calls.append(f"commit:{step}->{next_step}")

    async def enter_research(*_args, **_kwargs):
        calls.append("editorial_research")
        raise _ReachedEditorial

    monkeypatch.setattr(orchestrator, "load_current_run_corpus_view", current_view_for_run)
    monkeypatch.setattr(orchestrator, "current_attempt_has_triage_history", has_triage_history)
    monkeypatch.setattr(orchestrator, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(orchestrator, "prepare_v2_corpus", prepare)
    monkeypatch.setattr(orchestrator, "load_current_v2_corpus", load_current_must_not_run)
    monkeypatch.setattr(orchestrator, "_triage_prepared_corpus", triage)
    monkeypatch.setattr(orchestrator, "commit_step", commit)
    monkeypatch.setattr(orchestrator, "run_editorial_research_until_treatment", enter_research)

    with pytest.raises(_ReachedEditorial):
        await orchestrator.execute_generation_v2(run, db)  # type: ignore[arg-type]

    assert calls == [
        "load_current_view",
        "detect_triage_history",
        "load_legacy_workspace",
        "prepare_fresh_attempt",
        "commit:corpus_prepare->source_triage",
        "triage",
        "commit:source_triage->editorial_research",
        "editorial_research",
    ]


@pytest.mark.asyncio
async def test_triage_runner_with_prepared_corpus_does_not_prepare_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.pipeline import source_triage_stage

    run = _run()
    db = object()
    runtime = object()
    run_view = object()
    corpus = SimpleNamespace(run_view=run_view, provider_index=None)
    inventory = SourceMap()
    workspace = {"status": "open"}
    phases: list[object] = []

    async def prepare_must_not_run(*_args, **_kwargs):
        raise AssertionError("prepared corpus must be reused")

    async def start_phase(_run, _db, phase, _message, _pct):
        phases.append(phase)

    async def build_inventory(actual_db, actual_run_view):
        assert (actual_db, actual_run_view) == (db, run_view)
        return inventory

    async def ensure_workspace(actual_db, *, run: object, run_view: object, inventory: object):
        assert actual_db is db
        assert run is globals_run
        assert run_view is globals_run_view
        assert inventory is globals_inventory
        return workspace

    class FakeEvidenceToolset:
        def __init__(self, *, db: object, run: object, run_view: object, provider_index: object):
            assert db is globals_db
            assert run is globals_run
            assert run_view is globals_run_view
            assert provider_index is None

        def agent_tools(self):
            return ["evidence-tool"]

    globals_db = db
    globals_run = run
    globals_run_view = run_view
    globals_inventory = inventory
    monkeypatch.setattr(source_triage_stage, "prepare_v2_corpus", prepare_must_not_run)
    monkeypatch.setattr(source_triage_stage, "start_phase", start_phase)
    monkeypatch.setattr(source_triage_stage, "build_source_inventory", build_inventory)
    monkeypatch.setattr(source_triage_stage, "ensure_source_triage_workspace", ensure_workspace)
    monkeypatch.setattr(source_triage_stage, "EvidenceToolset", FakeEvidenceToolset)

    context = await source_triage_stage.SourceTriageStageRunner(
        run,
        db,
        runtime,
        prepared_corpus=corpus,  # type: ignore[arg-type]
    ).prepare_context()

    assert phases == [V2Phase.SOURCE_TRIAGE]
    assert context.corpus is corpus
    assert context.source_inventory is inventory
    assert context.workspace is workspace
    assert context.evidence_tools == ["evidence-tool"]


class _Result:
    def __init__(self, items: list[object]):
        self.items = items

    def scalar_one_or_none(self):
        assert len(self.items) <= 1
        return self.items[0] if self.items else None

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self.items)


class _ReadOnlyCorpusDb:
    def __init__(self, results: list[list[object]]):
        self.results = list(results)
        self.info: dict = {}
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        return _Result(self.results.pop(0))

    async def get(self, *_args, **_kwargs):
        raise AssertionError("provider index lookup was not expected")

    def add(self, _item):
        raise AssertionError("loading a corpus must not create an attempt")

    async def flush(self):
        raise AssertionError("loading a corpus must not write")

    async def commit(self):
        raise AssertionError("loading a corpus must not commit")


@pytest.mark.asyncio
async def test_load_current_v2_corpus_is_read_only_and_restores_attempt_scope() -> None:
    run = _run()
    run.status = "completed"
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    run_view = SimpleNamespace(
        id=uuid.uuid4(),
        generation_run_id=run.id,
        revision=7,
        attempt=3,
        status=V2RecordStatus.READY,
        provider_index_id=None,
        selected_source_ids=[str(source_id)],
        selected_source_version_ids=[str(version_id)],
        readiness_summary={"ready": True},
    )
    source = SimpleNamespace(id=source_id)
    version = SimpleNamespace(id=version_id)
    db = _ReadOnlyCorpusDb([[run_view], [source], [version]])

    result = await load_current_v2_corpus(db, run)  # type: ignore[arg-type]

    assert result.run_view is run_view
    assert result.sources == [source]
    assert result.source_versions == [version]
    assert result.readiness_summary == {"ready": True}
    assert db.execute_count == 3
    assert db.results == []
    scope = session_corpus_scope_or_none(db, run.id)
    assert scope is not None
    assert (scope.revision, scope.attempt) == (7, 3)


@pytest.mark.asyncio
async def test_unfinished_resume_rejects_failed_pinned_version() -> None:
    run = _run()
    run.status = "running"
    source = Source(id=uuid.uuid4(), project_id=run.project_id, source_type=SourceType.WEB)
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=run.project_id,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="legacy/shell.txt",
        status=V2RecordStatus.FAILED,
        is_active=False,
        metadata_={},
    )
    run_view = _pinned_run_view(run, source.id, version.id)
    db = _ReadOnlyCorpusDb([[run_view], [source], [version]])

    with pytest.raises(RuntimeError, match="unavailable target content"):
        await load_current_v2_corpus(db, run)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unfinished_resume_rejects_unavailable_pinned_pdf() -> None:
    run = _run()
    run.status = "running"
    source = Source(id=uuid.uuid4(), project_id=run.project_id, source_type=SourceType.PDF)
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=run.project_id,
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="legacy/report.pdf",
        status=V2RecordStatus.READY,
        is_active=False,
        metadata_={
            "content_assessment": {
                "schema_version": CONTENT_ASSESSMENT_SCHEMA_VERSION,
                "state": "unavailable",
                "reasons": ["invalid_pdf_structure"],
            }
        },
    )
    run_view = _pinned_run_view(run, source.id, version.id)
    db = _ReadOnlyCorpusDb([[run_view], [source], [version]])

    with pytest.raises(RuntimeError, match="unavailable target content"):
        await load_current_v2_corpus(db, run)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unfinished_resume_rejects_missing_pinned_version_row() -> None:
    run = _run()
    run.status = "running"
    source = Source(id=uuid.uuid4(), project_id=run.project_id, source_type=SourceType.WEB)
    missing_version_id = uuid.uuid4()
    run_view = _pinned_run_view(run, source.id, missing_version_id)
    db = _ReadOnlyCorpusDb([[run_view], [source], []])

    with pytest.raises(RuntimeError, match="pinned corpus rows are missing"):
        await load_current_v2_corpus(db, run)  # type: ignore[arg-type]


def _pinned_run_view(run: SimpleNamespace, source_id: uuid.UUID, version_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        generation_run_id=run.id,
        revision=1,
        attempt=1,
        status=V2RecordStatus.READY,
        provider_index_id=None,
        selected_source_ids=[str(source_id)],
        selected_source_version_ids=[str(version_id)],
        readiness_summary={"ready": True},
    )


@pytest.mark.asyncio
async def test_load_passed_triage_state_uses_current_revision_and_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.pipeline import triage_state_loader

    run = _run()
    db = object()
    run_view = SimpleNamespace(revision=5, attempt=2)
    corpus = SimpleNamespace(run_view=run_view, provider_index=None)
    source_map = SourceMap()
    inventory = SourceMap(gaps=["inventory marker"])

    def artifact(payload: dict, *, status: str = V2ArtifactStatus.PASSED) -> SimpleNamespace:
        return SimpleNamespace(id=uuid.uuid4(), payload=payload, status=status)

    source_map_artifact = artifact({**source_map.model_dump(mode="json"), "_metadata": {"ignored": True}})
    gate_artifact = artifact(SourceTriageGate(passed=True, summary="passed").model_dump(mode="json"))
    triage_artifact = artifact({})
    completion_artifact = artifact(SourceTriageCompletion(ready=True, summary="complete").model_dump(mode="json"))
    workspace_artifact = artifact({})
    artifacts = {
        (V2ArtifactType.SOURCE_MAP, "main"): source_map_artifact,
        (V2ArtifactType.SOURCE_TRIAGE, "gate"): gate_artifact,
        (V2ArtifactType.SOURCE_TRIAGE, "main"): triage_artifact,
        (V2ArtifactType.SOURCE_TRIAGE, "completion"): completion_artifact,
        (V2ArtifactType.SOURCE_TRIAGE_WORKSPACE, "main"): workspace_artifact,
    }
    artifact_calls: list[tuple[object, str, int, int]] = []

    async def load_corpus(actual_db, actual_run):
        assert (actual_db, actual_run) == (db, run)
        return corpus

    async def load_artifact(
        actual_db,
        run_id,
        *,
        artifact_type,
        artifact_key,
        corpus_revision,
        attempt,
    ):
        assert (actual_db, run_id) == (db, run.id)
        artifact_calls.append((artifact_type, artifact_key, corpus_revision, attempt))
        return artifacts.get((artifact_type, artifact_key))

    async def build_inventory(actual_db, actual_run_view):
        assert (actual_db, actual_run_view) == (db, run_view)
        return inventory

    async def related(actual_db, actual_run, *, revision, attempt):
        assert (actual_db, actual_run, revision, attempt) == (db, run, 5, 2)
        return {
            V2ArtifactType.EVIDENCE_ITEM: ["evidence-1"],
            V2ArtifactType.SOURCE_CARD: ["source-1"],
            V2ArtifactType.QUOTE_CARD: ["quote-1"],
        }

    class FakeEvidenceToolset:
        def __init__(self, *, db: object, run: object, run_view: object, provider_index: object):
            assert (db, run, run_view, provider_index) == (globals_db, globals_run, globals_run_view, None)

        def agent_tools(self):
            return ["hydrated-tool"]

    globals_db = db
    globals_run = run
    globals_run_view = run_view
    monkeypatch.setattr(triage_state_loader, "load_current_v2_corpus", load_corpus)
    monkeypatch.setattr(triage_state_loader, "load_current_artifact", load_artifact)
    monkeypatch.setattr(triage_state_loader, "build_source_inventory", build_inventory)
    monkeypatch.setattr(triage_state_loader, "_current_related_artifacts", related)
    monkeypatch.setattr(triage_state_loader, "EvidenceToolset", FakeEvidenceToolset)

    state = await triage_state_loader.load_passed_triage_state(db, run)  # type: ignore[arg-type]

    assert state.corpus is corpus
    assert state.source_inventory is inventory
    assert state.source_map == source_map
    assert state.triage_gate.passed is True
    assert state.triage_completion.ready is True
    assert state.evidence_tools == ["hydrated-tool"]
    assert state.evidence_artifact_ids == ["evidence-1"]
    assert state.source_artifact_ids == ["source-1"]
    assert state.quote_artifact_ids == ["quote-1"]
    assert state.workspace_artifact_id == str(workspace_artifact.id)
    assert all(call[2:] == (5, 2) for call in artifact_calls)
    assert len(artifact_calls) == 8
