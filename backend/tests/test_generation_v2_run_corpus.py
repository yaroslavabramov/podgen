from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.generations import start_generation
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.corpus_scope import set_session_corpus_scope
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace import ensure_source_triage_workspace
from app.models.generation_run import GenerationRun, GenerationStatus
from app.models.project import Project
from app.models.show_bible import ShowBible
from app.models.source import Source, SourceType
from app.schemas.generation import GenerationCreate


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return self.value


class _StartGenerationDb:
    def __init__(self, *, project: Project, bible: ShowBible, events: list[str]) -> None:
        self.results = [_Result(project), _Result(bible), _Result(0)]
        self.events = events
        self.added: list[object] = []

    async def execute(self, _statement):
        return self.results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if isinstance(item, GenerationRun):
                item.id = item.id or uuid.uuid4()
                item.status = item.status or GenerationStatus.QUEUED
                item.created_at = item.created_at or datetime.utcnow()

    async def refresh(self, _item):
        return None

    async def commit(self):
        self.events.append("commit")


class _EmptyScalarResult:
    def scalar_one_or_none(self):
        return None


class _ScopedPersistenceDb:
    def __init__(self) -> None:
        self.info: dict = {}
        self.added: list[object] = []

    async def execute(self, _statement):
        return _EmptyScalarResult()

    async def get(self, _model, _item_id):
        return None

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()


@pytest.mark.asyncio
async def test_start_generation_commits_run_before_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import generation_tasks

    events: list[str] = []
    user = SimpleNamespace(id=uuid.uuid4())
    project = Project(id=uuid.uuid4(), user_id=user.id, title="Test", language="en")
    bible = ShowBible(id=uuid.uuid4(), project_id=project.id, is_preset=False)
    db = _StartGenerationDb(project=project, bible=bible, events=events)

    monkeypatch.setattr(generation_tasks.run_generation_v2, "delay", lambda *_args, **_kwargs: events.append("enqueue"))

    await start_generation(
        project.id,
        GenerationCreate(
            duration_target=1800,
            user_prompt="Test topic",
            show_bible_id=bible.id,
            generation_mode="v2",
        ),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert events == ["commit", "enqueue"]


@pytest.mark.asyncio
async def test_start_generation_fails_explicitly_when_enqueue_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import generation_tasks

    events: list[str] = []
    user = SimpleNamespace(id=uuid.uuid4())
    project = Project(id=uuid.uuid4(), user_id=user.id, title="Test", language="en")
    bible = ShowBible(id=uuid.uuid4(), project_id=project.id, is_preset=False)
    db = _StartGenerationDb(project=project, bible=bible, events=events)

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(generation_tasks.run_generation_v2, "delay", fail_enqueue)

    with pytest.raises(HTTPException) as exc_info:
        await start_generation(
            project.id,
            GenerationCreate(
                duration_target=1800,
                user_prompt="Test topic",
                show_bible_id=bible.id,
                generation_mode="v2",
            ),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    run = next(item for item in db.added if isinstance(item, GenerationRun))
    assert exc_info.value.status_code == 503
    assert run.status == GenerationStatus.FAILED
    assert run.error_message == "Generation task could not be queued."
    assert events == ["commit", "commit"]


@pytest.mark.asyncio
async def test_failed_step_enqueue_is_retryable() -> None:
    from app.api.generations import _enqueue_saved_generation_run

    run = SimpleNamespace(
        id=uuid.uuid4(),
        status=GenerationStatus.QUEUED,
        error_message=None,
        completed_at=None,
        config={
            "execution_mode": "step",
            "step_control": {
                "requested_action": "next",
                "requested_target_node": "source_triage",
                "requested_at": "now",
                "last_node": "corpus_prepare",
            },
        },
    )

    class Db:
        commits = 0

        async def commit(self):
            self.commits += 1

    db = Db()

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("broker unavailable")

    with pytest.raises(HTTPException) as exc_info:
        await _enqueue_saved_generation_run(db, run, fail_enqueue)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503
    assert run.status == GenerationStatus.FAILED
    assert run.config["step_control"]["requested_action"] is None
    assert run.config["step_control"]["requested_target_node"] is None
    assert run.config["step_control"]["last_node"] == "corpus_prepare"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_explicit_source_selection_does_not_include_other_project_sources() -> None:
    from app.generation_v2.source_corpus_selection import load_run_sources

    project_id = uuid.uuid4()
    selected = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        ingestion_status="completed",
        is_temporary=False,
    )

    class Rows:
        def scalars(self):
            return self

        def __iter__(self):
            return iter([selected])

    class Db:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Rows()

    db = Db()
    run = SimpleNamespace(
        project_id=project_id,
        config={"source_ids": [str(selected.id)]},
    )

    sources = await load_run_sources(db, run)  # type: ignore[arg-type]
    query = str(db.statement)

    assert sources == [selected]
    assert "CAST(sources.id AS VARCHAR) IN" in query
    assert "sources.is_temporary IS false" not in query
    assert " OR " not in query


def test_v2_worker_retries_when_run_is_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import generation_tasks

    class RetryRequested(Exception):
        pass

    captured: dict[str, object] = {}

    async def missing_run(*_args, **_kwargs):
        return False

    def retry(**kwargs):
        captured.update(kwargs)
        raise RetryRequested

    monkeypatch.setattr(generation_tasks, "_run_generation_v2_async", missing_run)
    monkeypatch.setattr(generation_tasks.run_generation_v2, "retry", retry)

    with pytest.raises(RetryRequested):
        generation_tasks.run_generation_v2.run(str(uuid.uuid4()))

    assert generation_tasks.run_generation_v2.max_retries is None
    assert captured["countdown"] == 1
    assert captured["max_retries"] == 5
    assert "not committed yet" in str(captured["exc"])


def test_source_corpus_sync_retries_when_source_transaction_is_not_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import ingestion_tasks

    class RetryRequested(Exception):
        pass

    captured: dict[str, object] = {}

    async def missing_source(_source_id: str):
        return None

    def retry(**kwargs):
        captured.update(kwargs)
        raise RetryRequested

    monkeypatch.setattr(ingestion_tasks, "_sync_source_to_v2_corpus_async", missing_source)
    monkeypatch.setattr(ingestion_tasks.sync_source_to_v2_corpus, "retry", retry)

    with pytest.raises(RetryRequested):
        ingestion_tasks.sync_source_to_v2_corpus.run(str(uuid.uuid4()))

    assert ingestion_tasks.sync_source_to_v2_corpus.max_retries == 5
    assert captured["countdown"] == 1


@pytest.mark.asyncio
async def test_traces_and_evidence_inherit_current_corpus_scope() -> None:
    from app.generation_v2.tools.evidence_store import persist_evidence_item

    db = _ScopedPersistenceDb()
    run_id = uuid.uuid4()
    set_session_corpus_scope(
        db,
        run_id,
        SimpleNamespace(revision=3, attempt=2),
    )

    trace = await save_tool_trace(
        db,  # type: ignore[arg-type]
        generation_run_id=run_id,
        phase="v2_source_triage",
        agent_name="test",
        tool_name="test_tool",
        input_json={},
        output_json={},
    )
    evidence = await persist_evidence_item(
        db,  # type: ignore[arg-type]
        generation_run_id=run_id,
        project_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        retrieval_channel="local_chunk_search",
        snippet_text="Scoped evidence",
    )

    assert (trace.corpus_revision, trace.attempt) == (3, 2)
    assert (evidence.corpus_revision, evidence.attempt) == (3, 2)


@pytest.mark.asyncio
async def test_evidence_reads_resolve_current_scope_instead_of_failing_open() -> None:
    from app.generation_v2.source_corpus import list_evidence_dicts
    from app.models.generation_v2 import EvidenceItem, RunCorpusView

    run_id = uuid.uuid4()
    current_view = SimpleNamespace(revision=4, attempt=3)

    class Result:
        def __init__(self, *, scalar=None, rows=None):
            self.scalar = scalar
            self.rows = rows or []

        def scalar_one_or_none(self):
            return self.scalar

        def scalars(self):
            return self.rows

    class Db:
        def __init__(self):
            self.info: dict = {}
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            entity = statement.column_descriptions[0].get("entity")
            if entity is RunCorpusView:
                return Result(scalar=current_view)
            assert entity is EvidenceItem
            return Result(rows=[])

    db = Db()
    assert await list_evidence_dicts(db, run_id) == []  # type: ignore[arg-type]

    evidence_query = str(db.statements[-1])
    assert "evidence_items.corpus_revision" in evidence_query
    assert "evidence_items.attempt" in evidence_query
    assert db.info["v2_corpus_scopes"][str(run_id)].revision == 4
    assert db.info["v2_corpus_scopes"][str(run_id)].attempt == 3


def test_run_corpus_keeps_last_searchable_version_until_replacement_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_corpus_selection as selection
    from app.models.generation_v2 import (
        ProviderCorpusFile,
        ProviderCorpusIndex,
        SourceRepresentationKind,
        SourceVersion,
        V2RecordStatus,
    )

    project_id = uuid.uuid4()
    source = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        ingestion_status="completed",
        is_temporary=False,
    )
    old_version = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=project_id,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="versions/old.txt",
        content_hash="old",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=False,
        metadata_={},
    )
    replacement = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=project_id,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="versions/replacement.txt",
        content_hash="replacement",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.PENDING,
        is_active=True,
        metadata_={},
    )
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs-ready",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    old_provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_id=source.id,
        source_version_id=old_version.id,
        provider="openai",
        provider_file_id="file-old",
        provider_vector_store_file_id="vsf-old",
        status=V2RecordStatus.READY,
        attributes={
            "is_active": True,
            "agent_facing": True,
            "canonical_retrieval_surface": True,
        },
    )
    monkeypatch.setattr(selection, "_provider_uploads_required", lambda: True)

    usable_sources, usable_versions, provider_files, skipped, canonical_ids = selection.select_ready_run_corpus(
        [source],
        [old_version, replacement],
        [old_provider_file],
        provider_index,
    )

    assert usable_sources == [source]
    assert usable_versions == [old_version]
    assert provider_files == [old_provider_file]
    assert skipped == []
    assert canonical_ids == [str(old_version.id)]


@pytest.mark.asyncio
async def test_failed_source_version_does_not_replace_last_ready_version() -> None:
    from app.generation_v2.source_corpus_version_persistence import ensure_source_version
    from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus

    source = Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        ingestion_status="completed",
        is_temporary=False,
    )
    previous = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=source.project_id,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="versions/ready.txt",
        content_hash="ready",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )

    class Db:
        def __init__(self):
            self.results = [_Result(None), _Result(previous)]
            self.added: list[object] = []

        async def execute(self, _statement):
            return self.results.pop(0)

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            for item in self.added:
                if getattr(item, "id", None) is None:
                    item.id = uuid.uuid4()

    db = Db()
    failed = await ensure_source_version(
        db,  # type: ignore[arg-type]
        source,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="versions/failed.txt",
        content_hash="failed",
        metadata={},
        status=V2RecordStatus.FAILED,
    )

    assert failed.is_active is False
    assert failed.supersedes_source_version_id is None
    assert previous.is_active is True


@pytest.mark.asyncio
async def test_source_version_replacement_locks_the_source_row_on_postgres() -> None:
    from app.generation_v2.source_corpus_version_persistence import ensure_source_version
    from app.models.generation_v2 import SourceRepresentationKind

    source = Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        ingestion_status="completed",
        is_temporary=False,
    )

    class Db:
        def __init__(self):
            self.results = [_Result(source.id), _Result(None), _Result(None)]
            self.statements = []
            self.added = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement):
            self.statements.append(statement)
            return self.results.pop(0)

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            for item in self.added:
                if getattr(item, "id", None) is None:
                    item.id = uuid.uuid4()

    db = Db()
    await ensure_source_version(
        db,  # type: ignore[arg-type]
        source,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="versions/current.txt",
        content_hash="current",
        metadata={},
    )

    assert "FOR UPDATE" in str(db.statements[0])


@pytest.mark.asyncio
async def test_read_evidence_rejects_same_source_from_previous_corpus_attempt() -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    stale_evidence = SimpleNamespace(
        id=uuid.uuid4(),
        generation_run_id=run_id,
        corpus_revision=1,
        attempt=1,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    class Db:
        async def get(self, _model, item_id):
            return stale_evidence if item_id == stale_evidence.id else None

    toolset = EvidenceToolset(
        db=Db(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=uuid.uuid4()),
        run_view=SimpleNamespace(
            revision=1,
            attempt=2,
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
        provider_index=None,
    )

    result = await toolset.read_evidence_tool({"evidence_id": str(stale_evidence.id)})

    assert result == {"found": False}


@pytest.mark.asyncio
async def test_local_search_reads_pinned_inactive_version_snapshot_not_mutable_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_search as local_search_module
    from app.generation_v2.tools.evidence import EvidenceToolset
    from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus

    run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    old_version = SourceVersion(
        id=uuid.uuid4(),
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="snapshots/old-version.txt",
        content_hash="old-hash",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=False,
        metadata_={},
    )

    class Result:
        def scalars(self):
            return [old_version]

    class Db:
        async def execute(self, statement):
            assert statement.column_descriptions[0].get("entity") is SourceVersion
            assert all("is_active" not in str(clause) for clause in statement._where_criteria)
            return Result()

    persisted: list[dict] = []

    async def persist(_db, **kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), **kwargs)

    monkeypatch.setattr(local_search_module, "_source_version_text", lambda _version: "Old pinned truth remains here.")
    monkeypatch.setattr(local_search_module, "persist_evidence_item", persist)
    toolset = EvidenceToolset(
        db=Db(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            revision=1,
            attempt=1,
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(old_version.id)],
        ),
        provider_index=None,
    )

    results = await toolset._local_fallback_search(
        query="old pinned truth",
        purpose="source_triage",
        source_ids=[str(source_id)],
        limit=3,
    )

    assert len(results) == 1
    assert persisted[0]["source_version_id"] == old_version.id
    assert "Old pinned truth" in persisted[0]["snippet_text"]
    assert persisted[0]["metadata"] == {"immutable_source_version_search": True}


@pytest.mark.asyncio
async def test_triage_workspace_is_reset_for_a_new_corpus_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_triage_workspace as workspace_module

    existing = {
        "schema_version": "source_triage_workspace.v1",
        "manifest_hash": "same-manifest",
        "corpus_revision": 1,
        "attempt": 1,
        "status": "planned",
        "source_order": ["S01"],
        "source_sets": [{"set_id": "old", "status": "done"}],
    }
    saved: list[dict] = []

    async def load_workspace(*_args, **_kwargs):
        return existing

    async def save_workspace(*_args, **kwargs):
        saved.append(kwargs["workspace"])
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(workspace_module, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(workspace_module, "_source_order", lambda _inventory: ["S01"])

    workspace = await ensure_source_triage_workspace(
        None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        run_view=SimpleNamespace(manifest_hash="same-manifest", revision=1, attempt=2),
        inventory=SourceMap(),
    )

    assert workspace["status"] == "orientation"
    assert workspace["corpus_revision"] == 1
    assert workspace["attempt"] == 2
    assert workspace["source_sets"] == []
    assert saved


@pytest.mark.asyncio
async def test_research_workspace_is_reset_for_a_new_corpus_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as workspace_module

    existing = {
        "schema_version": workspace_module.WORKSPACE_SCHEMA_VERSION,
        "source_map_signature": "same-map",
        "corpus_revision": 1,
        "attempt": 1,
        "status": "finalized",
        "source_order": ["S01"],
        "investigation_jobs": [{"job_id": "old", "status": "mapped"}],
        "atom_drafts": [{"atom_id": "old-atom"}],
    }
    saved: list[dict] = []

    async def load_workspace(*_args, **_kwargs):
        return existing

    async def save_workspace(*_args, **kwargs):
        saved.append(kwargs["workspace"])
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(workspace_module, "load_research_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_research_workspace", save_workspace)
    monkeypatch.setattr(workspace_module, "_source_order", lambda _inventory: ["S01"])
    monkeypatch.setattr(workspace_module, "_source_map_signature", lambda *_args: "same-map")

    workspace = await workspace_module.ensure_research_workspace(
        None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        run_view=SimpleNamespace(revision=1, attempt=2),
        source_map=SourceMap(),
        source_inventory=SourceMap(),
    )

    assert workspace["status"] == "planning"
    assert workspace["corpus_revision"] == 1
    assert workspace["attempt"] == 2
    assert workspace["investigation_jobs"] == []
    assert workspace["atom_drafts"] == []
    assert saved


@pytest.mark.asyncio
async def test_url_read_rejects_oversized_source_title_instead_of_truncating() -> None:
    from app.generation_v2.source_corpus_url_versions import _persist_openai_url_read_version

    source = Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=SourceType.WEB,
        original_url="https://example.com/article",
        title=None,
    )

    with pytest.raises(ValueError, match="semantic_field_too_long.*source.title"):
        await _persist_openai_url_read_version(
            None,  # type: ignore[arg-type]
            source,
            {
                "title": "x" * 501,
                "read_status": "success",
                "extracted_text_markdown": "body",
            },
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert source.title is None
