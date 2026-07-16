from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.generation_v2.source_content_readiness import (
    CONTENT_STATE_CONFIRMED,
    CONTENT_STATE_UNAVAILABLE,
    CONTENT_STATE_UNCONFIRMED,
    CONTENT_ASSESSMENT_SCHEMA_VERSION,
    SourceContentAssessment,
    apply_redirect_target_guard,
    assess_media_source_content,
    assess_pdf_source_content,
    assess_web_source_content,
)
from app.config import settings
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source, SourceType
from app.services.storage import download_file, download_text, get_minio_client


_WEB_TEXT_KINDS = {
    str(SourceRepresentationKind.FETCHED_HTML),
    str(SourceRepresentationKind.RENDERED_SNAPSHOT),
    str(SourceRepresentationKind.MODEL_READ_TEXT),
    str(SourceRepresentationKind.EXTRACTED_TEXT),
    str(SourceRepresentationKind.PAGE_MARKED_TEXT),
}


def assess_run_content_rejections(
    sources: list[Source],
    source_versions: list[SourceVersion],
    *,
    storage_client: Any | None = None,
    download_file_fn: Callable[[Any, str], bytes] | None = None,
    download_text_fn: Callable[[Any, str], str] | None = None,
    storage_exists_fn: Callable[[Any, str], bool] | None = None,
    provider_ready_version_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Read-only compatibility check for selected source representations."""
    assessments = assess_run_content_versions(
        sources,
        source_versions,
        storage_client=storage_client,
        download_file_fn=download_file_fn,
        download_text_fn=download_text_fn,
        storage_exists_fn=storage_exists_fn,
        provider_ready_version_ids=provider_ready_version_ids,
        verify_persisted_storage=True,
    )
    return {
        version_id: list(assessment.reasons) or [assessment.state]
        for version_id, assessment in assessments.items()
        if not assessment.confirmed
    }


def assess_run_content_versions(
    sources: list[Source],
    source_versions: list[SourceVersion],
    *,
    storage_client: Any | None = None,
    download_file_fn: Callable[[Any, str], bytes] | None = None,
    download_text_fn: Callable[[Any, str], str] | None = None,
    storage_exists_fn: Callable[[Any, str], bool] | None = None,
    provider_ready_version_ids: set[str] | None = None,
    verify_persisted_storage: bool = False,
) -> dict[str, SourceContentAssessment]:
    """Assess stored content without mutating DB rows or source payloads."""
    sources_by_id = {source.id: source for source in sources}
    client = storage_client
    file_reader = download_file_fn or download_file
    text_reader = download_text_fn or download_text
    exists_reader = storage_exists_fn or _stored_object_exists
    provider_ready_ids = {str(value) for value in (provider_ready_version_ids or set())}
    assessments: dict[str, SourceContentAssessment] = {}

    for version in source_versions:
        version_id = str(version.id)
        if str(getattr(version, "status", "") or "") != str(V2RecordStatus.READY):
            assessments[version_id] = _synthetic_assessment(
                state=CONTENT_STATE_UNAVAILABLE,
                reason="source_version_not_ready",
                surface=str(getattr(version, "kind", "unknown")),
            )
            continue
        source = sources_by_id.get(getattr(version, "source_id", None))
        if source is None:
            assessments[version_id] = _synthetic_assessment(
                state=CONTENT_STATE_UNAVAILABLE,
                reason="source_missing",
                surface=str(getattr(version, "kind", "unknown")),
            )
            continue
        kind = str(version.kind)
        if kind == str(SourceRepresentationKind.ORIGINAL_URL):
            continue

        metadata = version.metadata_ if isinstance(version.metadata_, dict) else {}
        persisted = metadata.get("content_assessment")
        if (
            isinstance(persisted, dict)
            and persisted.get("state")
            and persisted.get("schema_version") == CONTENT_ASSESSMENT_SCHEMA_VERSION
        ):
            persisted_assessment = _persisted_assessment(persisted, surface=kind)
            if (
                persisted_assessment.confirmed
                and verify_persisted_storage
                and version_id not in provider_ready_ids
            ):
                if not version.storage_path:
                    persisted_assessment = _synthetic_assessment(
                        state=CONTENT_STATE_UNAVAILABLE,
                        reason="missing_stored_representation",
                        surface=kind,
                    )
                else:
                    try:
                        if client is None:
                            client = get_minio_client()
                        storage_exists = exists_reader(client, version.storage_path)
                    except Exception:
                        storage_exists = False
                    if not storage_exists:
                        persisted_assessment = _synthetic_assessment(
                            state=CONTENT_STATE_UNCONFIRMED,
                            reason="stored_representation_unreadable",
                            surface=kind,
                        )
            assessments[version_id] = persisted_assessment
            continue
        if not version.storage_path:
            assessments[version_id] = _synthetic_assessment(
                state=CONTENT_STATE_UNAVAILABLE,
                reason="missing_stored_representation",
                surface=kind,
            )
            continue

        try:
            if client is None:
                client = get_minio_client()
            if kind == str(SourceRepresentationKind.ORIGINAL_FILE):
                content = file_reader(client, version.storage_path)
                source_type = str(getattr(source, "source_type", "") or "")
                if (
                    source_type == str(SourceType.PDF)
                    or content.lstrip().startswith(b"%PDF-")
                    or "pdf" in str(metadata.get("content_type") or "").casefold()
                ):
                    assessment = assess_pdf_source_content(
                        content,
                        surface=kind,
                        content_type=str(metadata.get("content_type") or ""),
                        http_status=_status_code(metadata),
                    )
                elif source_type in {str(SourceType.AUDIO), str(SourceType.VIDEO)}:
                    assessment = assess_media_source_content(
                        content,
                        surface=kind,
                        media_type=source_type,
                    )
                else:
                    assessment = _binary_file_assessment(content, surface=kind)
            elif kind in _WEB_TEXT_KINDS or kind == str(SourceRepresentationKind.TRANSCRIPT):
                text = text_reader(client, version.storage_path)
                if str(getattr(source, "source_type", "") or "") == str(SourceType.WEB):
                    assessment = assess_web_source_content(
                        text,
                        surface=kind,
                        content_type=str(metadata.get("content_type") or ""),
                        http_status=_status_code(metadata),
                        input_is_markup=kind == str(SourceRepresentationKind.FETCHED_HTML),
                    )
                    assessment = apply_redirect_target_guard(
                        assessment,
                        requested_url=str(
                            metadata.get("requested_url") or getattr(source, "original_url", None) or ""
                        ),
                        final_url=str(
                            metadata.get("url")
                            or metadata.get("final_url")
                            or getattr(source, "original_url", None)
                            or ""
                        ),
                    )
                else:
                    assessment = _plain_text_assessment(text, surface=kind)
            else:
                assessment = _synthetic_assessment(
                    state=CONTENT_STATE_UNCONFIRMED,
                    reason="unassessed_web_representation",
                    surface=kind,
                )
                assessments[version_id] = assessment
                continue
        except Exception:
            assessments[version_id] = _synthetic_assessment(
                state=CONTENT_STATE_UNCONFIRMED,
                reason="stored_representation_unreadable",
                surface=kind,
            )
            continue

        assessments[version_id] = assessment

    return assessments


def _assessment_reasons(payload: dict[str, Any]) -> tuple[str, ...]:
    reasons = payload.get("reasons")
    if isinstance(reasons, list):
        normalized = [str(reason) for reason in reasons if str(reason).strip()]
        if normalized:
            return tuple(normalized)
    return (str(payload.get("state") or "content_unconfirmed"),)


def _persisted_assessment(payload: dict[str, Any], *, surface: str) -> SourceContentAssessment:
    state = str(payload.get("state") or CONTENT_STATE_UNCONFIRMED)
    if state not in {CONTENT_STATE_CONFIRMED, CONTENT_STATE_UNAVAILABLE, CONTENT_STATE_UNCONFIRMED}:
        state = CONTENT_STATE_UNCONFIRMED
    return SourceContentAssessment(
        state=state,
        reasons=_assessment_reasons(payload),
        char_count=_integer(payload.get("char_count")),
        non_whitespace_chars=_integer(payload.get("non_whitespace_chars")),
        surface=str(payload.get("surface") or surface),
        http_status=_optional_integer(payload.get("http_status")),
        content_type=str(payload.get("content_type") or ""),
    )


def _synthetic_assessment(*, state: str, reason: str, surface: str) -> SourceContentAssessment:
    return SourceContentAssessment(
        state=state,
        reasons=(reason,),
        char_count=0,
        non_whitespace_chars=0,
        surface=surface,
    )


def _plain_text_assessment(text: str, *, surface: str) -> SourceContentAssessment:
    stripped = str(text or "").strip()
    return SourceContentAssessment(
        state=CONTENT_STATE_CONFIRMED if stripped else CONTENT_STATE_UNCONFIRMED,
        reasons=() if stripped else ("empty_text",),
        char_count=len(text),
        non_whitespace_chars=len("".join(str(text or "").split())),
        surface=surface,
    )


def _binary_file_assessment(content: bytes, *, surface: str) -> SourceContentAssessment:
    return SourceContentAssessment(
        state=CONTENT_STATE_CONFIRMED if content else CONTENT_STATE_UNCONFIRMED,
        reasons=() if content else ("empty_file",),
        char_count=len(content),
        non_whitespace_chars=len(content),
        surface=surface,
    )


def _stored_object_exists(client: Any, storage_path: str) -> bool:
    client.stat_object(settings.minio_bucket, storage_path)
    return True


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_code(metadata: dict[str, Any]) -> int | None:
    value = metadata.get("status_code")
    if value is None:
        value = metadata.get("http_status")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["assess_run_content_rejections", "assess_run_content_versions"]
