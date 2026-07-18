from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.runtime_tool_specs import runtime_tool_specs
from app.generation_v2.runtime_contracts import ToolPolicy
from app.config import settings
from app.generation_v2.source_corpus import prepare_v2_corpus
from app.generation_v2.source_corpus_content_preflight import assess_run_content_rejections
from app.generation_v2.source_content_readiness import CONTENT_ASSESSMENT_SCHEMA_VERSION
from app.generation_v2.source_corpus_hosted_search import prepare_run_hosted_file_search
from app.generation_v2.source_corpus_versions import ensure_source_versions
from app.generation_v2.tools.evidence_provider_utils import provider_filters
from app.generation_v2.tools.evidence_local_source import EvidenceLocalSourceMixin
from app.generation_v2.tools.evidence_provider_search import EvidenceProviderSearchMixin
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex
from app.models.source import IngestionStatus, Source, SourceType


def _source(source_type: SourceType, **values: object) -> Source:
    return Source(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_type=source_type,
        **values,
    )


def _legacy_version(source: Source, kind: SourceRepresentationKind, path: str) -> SourceVersion:
    return SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=source.project_id,
        kind=kind,
        storage_path=path,
        content_hash=uuid.uuid4().hex,
        metadata_={},
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
    )


def test_fetched_html_read_surface_verifies_capture_then_extracts_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_source_text

    source = _source(SourceType.WEB, original_url="https://example.com/report")
    version = _legacy_version(source, SourceRepresentationKind.FETCHED_HTML, "legacy/report.html")
    html = "<html><body><main>The complete immutable article.</main></body></html>"
    version.content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    monkeypatch.setattr(evidence_source_text, "get_minio_client", lambda: object())
    monkeypatch.setattr(evidence_source_text, "download_text", lambda *_args: html)
    monkeypatch.setattr(
        evidence_source_text,
        "extract_web_document_text",
        lambda stored: "The complete immutable article." if stored == html else "",
    )

    assert evidence_source_text.source_version_text(version) == "The complete immutable article."


def test_model_read_surface_removes_only_internal_source_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_source_text

    source = _source(SourceType.PDF)
    version = _legacy_version(source, SourceRepresentationKind.PAGE_MARKED_TEXT, "page.txt")
    factual_uuid = str(uuid.uuid4())
    stored = (
        f"[source_id={source.id} page_marker=1 paragraph=1] "
        f"The filing itself names factual id {factual_uuid}."
    )
    version.content_hash = hashlib.sha256(stored.encode("utf-8")).hexdigest()
    monkeypatch.setattr(evidence_source_text, "get_minio_client", lambda: object())
    monkeypatch.setattr(evidence_source_text, "download_text", lambda *_args: stored)

    text = evidence_source_text.source_version_text(version)

    assert str(source.id) not in text
    assert text.startswith("[page_marker=1 paragraph=1]")
    assert factual_uuid in text


def test_legacy_url_media_transcript_is_not_treated_as_unassessed_web_surface() -> None:
    source = _source(SourceType.VIDEO, original_url="https://video.example/watch/1")
    transcript = _legacy_version(source, SourceRepresentationKind.TRANSCRIPT, "legacy/transcript.txt")

    rejections = assess_run_content_rejections(
        [source],
        [transcript],
        storage_client=object(),
        download_text_fn=lambda _client, _path: (
            "A complete interview transcript with a clear argument, supporting examples, and a conclusion. " * 4
        ),
    )

    assert rejections == {}


def test_legacy_pdf_is_structurally_assessed_instead_of_auto_confirmed() -> None:
    source = _source(SourceType.PDF, file_path="legacy/report.pdf")
    version = _legacy_version(source, SourceRepresentationKind.ORIGINAL_FILE, "legacy/report.pdf")

    rejections = assess_run_content_rejections(
        [source],
        [version],
        storage_client=object(),
        download_file_fn=lambda *_args: b"%PDF-1.7\n" + b"x" * 40,
    )

    assert rejections[str(version.id)] == ["invalid_pdf_structure"]


def test_legacy_media_file_and_transcript_are_assessed_independently() -> None:
    source = _source(SourceType.AUDIO, file_path="legacy/clip.mp3", raw_text_path="legacy/clip.txt")
    original = _legacy_version(source, SourceRepresentationKind.ORIGINAL_FILE, "legacy/clip.mp3")
    transcript = _legacy_version(source, SourceRepresentationKind.TRANSCRIPT, "legacy/clip.txt")

    rejections = assess_run_content_rejections(
        [source],
        [original, transcript],
        storage_client=object(),
        download_file_fn=lambda *_args: b"not audio",
        download_text_fn=lambda *_args: "A short but valid transcript.",
    )

    assert rejections == {str(original.id): ["invalid_media_signature"]}


def test_legacy_web_redirect_metadata_is_applied_during_preflight() -> None:
    source = _source(SourceType.WEB, original_url="https://publisher.example/article/123")
    version = _legacy_version(source, SourceRepresentationKind.RENDERED_SNAPSHOT, "legacy/rendered.txt")
    version.metadata_ = {
        "requested_url": source.original_url,
        "url": "https://auth.example.com/",
    }

    rejections = assess_run_content_rejections(
        [source],
        [version],
        storage_client=object(),
        download_text_fn=lambda *_args: (
            "A detailed report explains the history, mechanism, evidence, consequences, and limitations. " * 20
        ),
    )

    assert rejections[str(version.id)] == ["redirect_to_access_page"]


def test_persisted_confirmation_requires_local_or_exact_provider_surface() -> None:
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    version = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "missing/report.txt")
    version.metadata_ = {
        "content_assessment": {
            "schema_version": CONTENT_ASSESSMENT_SCHEMA_VERSION,
            "state": "confirmed",
            "reasons": [],
            "surface": "extracted_text",
            "char_count": 800,
            "non_whitespace_chars": 700,
        }
    }

    missing = assess_run_content_rejections(
        [source],
        [version],
        storage_client=object(),
        storage_exists_fn=lambda *_args: False,
    )
    provider_backed = assess_run_content_rejections(
        [source],
        [version],
        storage_client=object(),
        storage_exists_fn=lambda *_args: False,
        provider_ready_version_ids={str(version.id)},
    )

    assert missing[str(version.id)] == ["stored_representation_unreadable"]
    assert provider_backed == {}


@pytest.mark.asyncio
async def test_explicit_local_read_refuses_disqualified_pinned_version() -> None:
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    version = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/shell.txt")
    version.metadata_ = {
        "content_assessment": {
            "schema_version": CONTENT_ASSESSMENT_SCHEMA_VERSION,
            "state": "unavailable",
            "reasons": ["cookie_consent_only"],
        }
    }
    owner = _LocalResolverOwner(source, version)

    resolved = await owner._resolve_source_version(
        source_id=str(source.id),
        source_version_id=str(version.id),
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_implicit_local_read_keeps_confirmed_immutable_fallback_only() -> None:
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    confirmed = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/report.txt")
    confirmed.is_active = False
    rejected = _legacy_version(source, SourceRepresentationKind.RENDERED_SNAPSHOT, "legacy/shell.txt")
    rejected.metadata_ = {
        "content_assessment": {
            "schema_version": CONTENT_ASSESSMENT_SCHEMA_VERSION,
            "state": "unavailable",
            "reasons": ["access_form_only"],
        }
    }
    owner = _LocalResolverOwner(source, confirmed, query_versions=[rejected, confirmed])

    resolved = await owner._resolve_source_version(source_id=str(source.id), source_version_id="")

    assert resolved is confirmed


@pytest.mark.asyncio
async def test_implicit_local_read_recovers_fuller_view_from_pinned_html_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source

    source = _source(SourceType.WEB, original_url="https://example.com/report")
    extracted = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/report.md")
    fetched = _legacy_version(source, SourceRepresentationKind.FETCHED_HTML, "legacy/report.html")
    extracted.metadata_ = {"derived_from_source_version_id": str(fetched.id)}
    appendix = "Appendix references supporting the report. " * 180
    complete = (
        "The full article explains the historical decision, the operating mechanism, and its consequences. " * 700
        + appendix
    )
    owner = _LocalResolverOwner(source, extracted, query_versions=[extracted, fetched])
    monkeypatch.setattr(
        evidence_local_source,
        "_source_version_text",
        lambda version: appendix if version is extracted else complete,
    )

    resolved = await owner._resolve_source_version(source_id=str(source.id), source_version_id="")

    assert resolved is fetched


@pytest.mark.asyncio
async def test_implicit_local_read_rejects_long_parent_shell_that_drops_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source

    source = _source(SourceType.WEB, original_url="https://example.com/report")
    extracted = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/report.md")
    fetched = _legacy_version(source, SourceRepresentationKind.FETCHED_HTML, "legacy/report.html")
    extracted.metadata_ = {"derived_from_source_version_id": str(fetched.id)}
    article = " ".join(f"articleterm{index:03d}" for index in range(220))
    navigation_shell = " ".join(f"navigationitem{index:04d}" for index in range(1400))
    owner = _LocalResolverOwner(source, extracted, query_versions=[extracted, fetched])
    monkeypatch.setattr(
        evidence_local_source,
        "_source_version_text",
        lambda version: article if version is extracted else navigation_shell,
    )

    resolved = await owner._resolve_source_version(source_id=str(source.id), source_version_id="")

    assert resolved is extracted


@pytest.mark.asyncio
async def test_provider_result_is_ignored_after_pinned_version_is_disqualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    version = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/shell.txt")
    version.metadata_ = {
        "content_assessment": {
            "schema_version": CONTENT_ASSESSMENT_SCHEMA_VERSION,
            "state": "unavailable",
            "reasons": ["cookie_consent_only"],
        }
    }
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=source.project_id,
        provider="openai",
        provider_vector_store_id="vs-test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_id=source.id,
        source_version_id=version.id,
        provider="openai",
        provider_file_id="file-stale",
        status=V2RecordStatus.READY,
        attributes={
            "is_active": False,
            "agent_facing": True,
            "canonical_retrieval_surface": True,
            "content_confirmed": False,
        },
    )
    owner = _ProviderResultOwner(provider_index, provider_file, version)

    async def must_not_persist(*_args, **_kwargs):
        raise AssertionError("disqualified provider result must not become evidence")

    monkeypatch.setattr(
        "app.generation_v2.tools.evidence_provider_search.persist_evidence_item",
        must_not_persist,
    )
    result = await owner._persist_provider_result(
        SimpleNamespace(
            attributes={"source_id": str(source.id), "source_version_id": str(version.id)},
            content=[{"text": "Cookie settings and controls."}],
            file_id="file-stale",
            filename="shell.txt",
            score=0.9,
        ),
        query="topic",
        purpose="triage",
        result_index=0,
    )

    assert result is None


@pytest.mark.asyncio
async def test_url_media_short_transcript_does_not_use_web_page_length_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        SourceType.AUDIO,
        original_url="https://audio.example/clip/1",
        raw_text_path="raw/clip.txt",
    )
    calls: list[dict[str, object]] = []

    async def persist(_db, current_source, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            source_id=current_source.id,
            project_id=current_source.project_id,
            kind=kwargs["kind"],
            status=kwargs.get("status", V2RecordStatus.READY),
            is_active=kwargs.get("status", V2RecordStatus.READY) == V2RecordStatus.READY,
            metadata_=kwargs["metadata"],
        )

    monkeypatch.setattr("app.generation_v2.source_corpus_versions.ensure_source_version", persist)
    monkeypatch.setattr(
        "app.generation_v2.source_corpus_versions.ensure_openai_url_read_versions",
        lambda *_args, **_kwargs: _async_result([]),
    )
    monkeypatch.setattr(
        "app.generation_v2.source_corpus_versions.ensure_web_snapshot_versions",
        lambda *_args, **_kwargs: _async_result([]),
    )
    monkeypatch.setattr("app.generation_v2.source_corpus_versions.get_minio_client", lambda: object())
    monkeypatch.setattr(
        "app.generation_v2.source_corpus_versions.download_text",
        lambda *_args: "A short but valid clip transcript.",
    )
    monkeypatch.setattr("app.generation_v2.source_corpus_versions.upload_text", lambda *_args: None)

    await ensure_source_versions(SimpleNamespace(flush=lambda: _async_result(None)), source)  # type: ignore[arg-type]

    transcript_call = next(call for call in calls if call["kind"] == SourceRepresentationKind.TRANSCRIPT)
    assert transcript_call["status"] == V2RecordStatus.READY
    assert transcript_call["metadata"]["content_assessment"]["state"] == "confirmed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_pdf_original_file_requires_pdf_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(SourceType.PDF, file_path="uploads/not-a-pdf.pdf", title="not-a-pdf.pdf")
    calls: list[dict[str, object]] = []

    async def persist(_db, current_source, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            source_id=current_source.id,
            kind=kwargs["kind"],
            status=kwargs["status"],
            is_active=kwargs["status"] == V2RecordStatus.READY,
            metadata_=kwargs["metadata"],
        )

    monkeypatch.setattr("app.generation_v2.source_corpus_versions.ensure_source_version", persist)
    monkeypatch.setattr("app.generation_v2.source_corpus_versions.get_minio_client", lambda: object())
    monkeypatch.setattr(
        "app.generation_v2.source_corpus_versions.download_file",
        lambda *_args: b"<html><body>403 Access denied</body></html>",
    )

    await ensure_source_versions(SimpleNamespace(flush=lambda: _async_result(None)), source)  # type: ignore[arg-type]

    assert calls[0]["kind"] == SourceRepresentationKind.ORIGINAL_FILE
    assert calls[0]["status"] == V2RecordStatus.FAILED
    assert calls[0]["metadata"]["content_assessment"]["state"] == "unavailable"  # type: ignore[index]
    assert "invalid_pdf_signature" in calls[0]["metadata"]["content_assessment"]["reasons"]  # type: ignore[index]


def test_hosted_search_requires_confirmed_content_and_scoped_search_is_version_pinned() -> None:
    source_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    hosted = runtime_tool_specs(
        vector_store_id="vs-test",
        local_tools=[],
        policy=ToolPolicy(
            file_search=True,
            web_search=False,
            local_tools=False,
            file_search_source_version_ids=(version_id,),
        ),
    )[0]["filters"]["filters"]
    scoped = provider_filters([source_id], [version_id])["filters"]
    required = {"type": "eq", "key": "content_confirmed", "value": True}

    assert required in hosted
    assert {"type": "in", "key": "source_version_id", "value": [version_id]} in hosted
    assert {"type": "eq", "key": "source_id", "value": source_id} in scoped
    assert {"type": "eq", "key": "source_version_id", "value": version_id} in scoped


def test_hosted_search_is_disabled_without_an_exact_version_scope() -> None:
    tools = runtime_tool_specs(
        vector_store_id="vs-test",
        local_tools=[],
        policy=ToolPolicy(file_search=True, web_search=False, local_tools=False),
    )

    assert tools == []


@pytest.mark.asyncio
async def test_legacy_valid_provider_file_is_force_backfilled_before_hosted_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    project_id = uuid.uuid4()
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    source.project_id = project_id
    version = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/report.txt")
    version.is_active = False  # provider-ready fallback pinned by the immutable run view
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs-old",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_id=source.id,
        source_version_id=version.id,
        provider="openai",
        provider_file_id="file-old-valid",
        status=V2RecordStatus.READY,
        attributes={
            "is_active": True,
            "agent_facing": True,
            "canonical_retrieval_surface": True,
        },
    )
    db = _HostedBackfillDb([provider_file], [version])
    calls: list[dict[str, object]] = []

    async def sync(_index, _file, attributes, *, force=False):
        calls.append({"attributes": attributes, "force": force})
        return True

    version_ids = await prepare_run_hosted_file_search(
        db,  # type: ignore[arg-type]
        provider_index,
        SimpleNamespace(
            provider_index_id=provider_index.id,
            eligible_provider_file_ids=[provider_file.provider_file_id],
            selected_source_version_ids=[str(version.id)],
        ),  # type: ignore[arg-type]
        sync_attributes_fn=sync,
    )

    assert version_ids == [str(version.id)]
    assert calls[0]["force"] is True
    assert calls[0]["attributes"]["content_confirmed"] is True  # type: ignore[index]
    assert calls[0]["attributes"]["is_active"] is True  # type: ignore[index]
    assert provider_file.attributes["content_confirmed"] is True
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_legacy_valid_provider_file_stays_local_when_remote_backfill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    project_id = uuid.uuid4()
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    source.project_id = project_id
    version = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/report.txt")
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs-old",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_id=source.id,
        source_version_id=version.id,
        provider="openai",
        provider_file_id="file-old-valid",
        status=V2RecordStatus.READY,
        attributes={"is_active": True, "agent_facing": True, "canonical_retrieval_surface": True},
    )
    db = _HostedBackfillDb([provider_file], [version])

    async def sync(*_args, **_kwargs):
        return False

    version_ids = await prepare_run_hosted_file_search(
        db,  # type: ignore[arg-type]
        provider_index,
        SimpleNamespace(
            provider_index_id=provider_index.id,
            eligible_provider_file_ids=[provider_file.provider_file_id],
            selected_source_version_ids=[str(version.id)],
        ),  # type: ignore[arg-type]
        sync_attributes_fn=sync,
    )

    assert version_ids == []
    assert "content_confirmed" not in provider_file.attributes
    assert db.flushes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_succeeds", [False, True])
async def test_unconfirmed_content_never_becomes_hosted_even_if_attribute_sync_would_succeed(
    sync_succeeds: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    project_id = uuid.uuid4()
    source = _source(SourceType.WEB, original_url="https://example.com/report")
    source.project_id = project_id
    version = _legacy_version(source, SourceRepresentationKind.EXTRACTED_TEXT, "legacy/shell.txt")
    version.metadata_ = {
        "content_assessment": {
            "state": "unavailable",
            "reasons": ["cookie_consent_only"],
        }
    }
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs-old",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_id=source.id,
        source_version_id=version.id,
        provider="openai",
        provider_file_id="file-old-bad",
        status=V2RecordStatus.READY,
        attributes={},
    )
    db = _HostedBackfillDb([provider_file], [version])
    calls = 0

    async def sync(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return sync_succeeds

    version_ids = await prepare_run_hosted_file_search(
        db,  # type: ignore[arg-type]
        provider_index,
        SimpleNamespace(
            provider_index_id=provider_index.id,
            eligible_provider_file_ids=[provider_file.provider_file_id],
            selected_source_version_ids=[str(version.id)],
        ),  # type: ignore[arg-type]
        sync_attributes_fn=sync,
    )

    assert version_ids == []
    assert calls == 0
    assert "content_confirmed" not in provider_file.attributes
    assert db.flushes == 0


@pytest.mark.asyncio
async def test_fact_citation_backfills_before_enabling_hosted_search(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.pipeline import post_drafting_stage
    from app.generation_v2.schemas import ClaimCitationBundle

    events: list[str] = []
    version_id = str(uuid.uuid4())
    provider_index = SimpleNamespace(id=uuid.uuid4(), provider_vector_store_id="vs-old")

    async def start(*_args, **_kwargs):
        events.append("start")

    async def prepare(_db, current_index, _run_view):
        assert current_index is provider_index
        events.append("backfill")
        return [version_id]

    async def extract(*_args, **kwargs):
        events.append("hosted_call")
        assert kwargs["vector_store_id"] == "vs-old"
        assert kwargs["hosted_source_version_ids"] == [version_id]
        return ClaimCitationBundle(claims=[], citation_links=[])

    async def list_evidence(*_args, **_kwargs):
        return []

    async def validate(*_args, **_kwargs):
        return "saved"

    monkeypatch.setattr(post_drafting_stage, "start_phase", start)
    monkeypatch.setattr(post_drafting_stage, "prepare_run_hosted_file_search", prepare)
    monkeypatch.setattr(post_drafting_stage, "extract_and_link_claims", extract)
    monkeypatch.setattr(post_drafting_stage, "list_evidence_dicts", list_evidence)
    monkeypatch.setattr(post_drafting_stage, "validate_and_save_claim_bundle", validate)

    result = await post_drafting_stage.extract_validate_claim_bundle(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4()),  # type: ignore[arg-type]
        SimpleNamespace(
            prompt_hash_for=lambda *_args: "prompt-hash",
            fallback_metadata_for=lambda *_args: {},
        ),  # type: ignore[arg-type]
        source_cards=[],
        quote_cards=[],
        evidence_toolset=SimpleNamespace(
            provider_index=provider_index,
            persist_hosted_file_search_calls=lambda *_args: None,
        ),
        evidence_tools=[],
        dossiers=[],
        drafts=[],
        critique=SimpleNamespace(),
        critique_artifact_id="critique-artifact",
        draft_artifact_ids_by_act={},
        source_artifact_ids=[],
        quote_artifact_ids=[],
        vector_store_id="vs-old",
        run_view=SimpleNamespace(),
        show_bible=SimpleNamespace(),  # type: ignore[arg-type]
        pass_key="initial",
    )

    assert result == "saved"
    assert events == ["start", "backfill", "hosted_call"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ingestion_status",
    [IngestionStatus.PENDING, IngestionStatus.ENQUEUED, IngestionStatus.PROCESSING],
)
async def test_prepare_rejects_source_before_ingestion_completes(
    monkeypatch: pytest.MonkeyPatch,
    ingestion_status: IngestionStatus,
) -> None:
    source = _source(
        SourceType.WEB,
        original_url="https://example.com/report",
        ingestion_status=ingestion_status,
        is_temporary=False,
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=source.project_id,
        config={"source_ids": [str(source.id)]},
    )

    async def load_sources(_db, _run):
        return [source]

    async def provider_lookup_must_not_run(*_args, **_kwargs):
        raise AssertionError("unfinished source must fail before corpus/provider lookup")

    monkeypatch.setattr("app.generation_v2.source_corpus._load_run_sources", load_sources)
    monkeypatch.setattr(
        "app.generation_v2.source_corpus._load_project_provider_index",
        provider_lookup_must_not_run,
    )

    with pytest.raises(RuntimeError, match="selected sources are not ready"):
        await prepare_v2_corpus(SimpleNamespace(), run)  # type: ignore[arg-type]


async def _async_result(value):
    return value


class _LocalResolverResult:
    def __init__(self, versions: list[SourceVersion]) -> None:
        self.versions = versions

    def scalars(self):
        return list(self.versions)


class _LocalResolverDb:
    def __init__(self, version: SourceVersion, query_versions: list[SourceVersion]) -> None:
        self.version = version
        self.query_versions = query_versions

    async def get(self, _model, _version_id):
        return self.version

    async def execute(self, _statement):
        return _LocalResolverResult(self.query_versions)


class _LocalResolverOwner(EvidenceLocalSourceMixin):
    def __init__(
        self,
        source: Source,
        version: SourceVersion,
        *,
        query_versions: list[SourceVersion] | None = None,
    ) -> None:
        self.db = _LocalResolverDb(version, query_versions or [version])
        self.run_view = SimpleNamespace(
            selected_source_ids=[str(source.id)],
            selected_source_version_ids=[str(item.id) for item in (query_versions or [version])],
        )

    async def _triage_scope_error_for_source_ids(self, _source_ids):
        return None

    async def _source_version_id_from_ref(self, _version_ref):
        return None

    async def _source_id_from_ref_or_id(self, source_ref):
        return source_ref


class _ProviderResultDb:
    def __init__(self, provider_file: ProviderCorpusFile, version: SourceVersion) -> None:
        self.provider_file = provider_file
        self.version = version

    async def execute(self, _statement):
        return _ProviderResultRow(self.provider_file)

    async def get(self, _model, _version_id):
        return self.version


class _ProviderResultRow:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ProviderResultOwner(EvidenceProviderSearchMixin):
    def __init__(
        self,
        provider_index: ProviderCorpusIndex,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
    ) -> None:
        self.db = _ProviderResultDb(provider_file, version)
        self.provider_index = provider_index
        self.run = SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id)
        self.run_view = SimpleNamespace(
            selected_source_ids=[str(version.source_id)],
            selected_source_version_ids=[str(version.id)],
            eligible_provider_file_ids=[provider_file.provider_file_id],
        )

    def _in_run_corpus_view(self, source_id, source_version_id):
        return str(source_id) in self.run_view.selected_source_ids and str(
            source_version_id
        ) in self.run_view.selected_source_version_ids


class _HostedBackfillResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self):
        return list(self.values)


class _HostedBackfillDb:
    def __init__(self, provider_files: list[ProviderCorpusFile], versions: list[SourceVersion]) -> None:
        self.provider_files = provider_files
        self.versions = versions
        self.flushes = 0

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is ProviderCorpusFile:
            return _HostedBackfillResult(self.provider_files)
        if entity is SourceVersion:
            return _HostedBackfillResult(self.versions)
        return _HostedBackfillResult([])

    async def flush(self):
        self.flushes += 1
