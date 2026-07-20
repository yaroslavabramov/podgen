from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from app.config import settings
from app.ingestion.web_quality import WebQualityAssessment, assess_web_fetch_health
from app.models.generation_v2 import SourceRepresentationKind, V2RecordStatus


CONTENT_ASSESSMENT_SCHEMA_VERSION = "source_content_assessment.v2"
_COMPATIBLE_CONTENT_ASSESSMENT_SCHEMA_VERSIONS = {
    "source_content_assessment.v1",
    CONTENT_ASSESSMENT_SCHEMA_VERSION,
}
CONTENT_STATE_CONFIRMED = "confirmed"
CONTENT_STATE_UNAVAILABLE = "unavailable"
CONTENT_STATE_UNCONFIRMED = "unconfirmed"
_CONTENT_STATES = {
    CONTENT_STATE_CONFIRMED,
    CONTENT_STATE_UNAVAILABLE,
    CONTENT_STATE_UNCONFIRMED,
}
_MARKUP_CONTENT_TYPES = ("html", "xml", "xhtml")
_MARKUP_PREFIX_RE = re.compile(r"^\s*(?:<!doctype\s+html|<html\b|<body\b|<article\b|<main\b)", re.IGNORECASE)
_ACCESS_REDIRECT_SEGMENTS = {
    "auth",
    "authenticate",
    "authorization",
    "authorize",
    "consent",
    "cookie-consent",
    "login",
    "sign-in",
    "signin",
    "sso",
}
_ACCESS_REDIRECT_HOST_LABELS = {
    "accounts",
    "auth",
    "idp",
    "identity",
    "login",
    "signin",
    "sso",
}


@dataclass(frozen=True, slots=True)
class SourceContentAssessment:
    state: str
    reasons: tuple[str, ...]
    char_count: int
    non_whitespace_chars: int
    surface: str
    http_status: int | None = None
    content_type: str = ""

    @property
    def confirmed(self) -> bool:
        return self.state == CONTENT_STATE_CONFIRMED


def assess_web_source_content(
    text: str,
    *,
    surface: str,
    http_status: int | None = None,
    content_type: str = "",
    min_chars: int | None = None,
    title: str | None = None,
    input_is_markup: bool | None = None,
) -> SourceContentAssessment:
    """Confirm that a web capture contains document text, not a transport/UI response."""
    probe = visible_web_text(
        text,
        content_type=content_type,
        input_is_markup=input_is_markup,
    )
    minimum = max(1, int(min_chars or settings.web_quality_gate_a_min_chars))
    combined_probe = f"{title.strip()}\n{probe}".strip() if title and title.strip() else probe
    signature_health = assess_web_fetch_health(combined_probe, min_chars=1)
    health = signature_health if signature_health.state == "blocked" else assess_web_fetch_health(
        probe,
        min_chars=minimum,
    )
    if http_status is not None and http_status >= 400:
        if health.state == "ok":
            return SourceContentAssessment(
                state=CONTENT_STATE_CONFIRMED,
                reasons=(f"http_status_{http_status}",),
                char_count=health.char_count,
                non_whitespace_chars=health.non_whitespace_chars,
                surface=surface,
                http_status=http_status,
                content_type=content_type,
            )
        return SourceContentAssessment(
            state=CONTENT_STATE_UNAVAILABLE,
            reasons=tuple(dict.fromkeys([f"http_status_{http_status}", *health.reasons])),
            char_count=health.char_count,
            non_whitespace_chars=health.non_whitespace_chars,
            surface=surface,
            http_status=http_status,
            content_type=content_type,
        )
    return _from_web_health(
        health,
        surface=surface,
        http_status=http_status,
        content_type=content_type,
    )


def assess_pdf_source_content(
    content: bytes,
    *,
    surface: str,
    http_status: int | None = None,
    content_type: str = "",
) -> SourceContentAssessment:
    """Validate the transport-level identity of a fetched PDF before promotion."""
    if not content.lstrip().startswith(b"%PDF-"):
        state = CONTENT_STATE_UNAVAILABLE
        reasons = tuple(
            dict.fromkeys(
                [
                    *([f"http_status_{http_status}"] if http_status is not None and http_status >= 400 else []),
                    "invalid_pdf_signature",
                ]
            )
        )
    elif len(content) < 32:
        state = CONTENT_STATE_UNCONFIRMED
        reasons = tuple(
            dict.fromkeys(
                [
                    *([f"http_status_{http_status}"] if http_status is not None and http_status >= 400 else []),
                    "pdf_too_short",
                ]
            )
        )
    else:
        try:
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(content)
            try:
                page_count = len(document)
            finally:
                document.close()
        except Exception as exc:
            if "password" in str(exc).casefold():
                state = CONTENT_STATE_UNCONFIRMED
                reasons = ("encrypted_pdf_unreadable",)
            else:
                state = CONTENT_STATE_UNAVAILABLE
                reasons = ("invalid_pdf_structure",)
        else:
            state = CONTENT_STATE_CONFIRMED if page_count > 0 else CONTENT_STATE_UNCONFIRMED
            reasons = () if page_count > 0 else ("pdf_has_no_pages",)
            if http_status is not None and http_status >= 400:
                reasons = tuple(dict.fromkeys([f"http_status_{http_status}", *reasons]))
    return SourceContentAssessment(
        state=state,
        reasons=reasons,
        char_count=len(content),
        non_whitespace_chars=len(content),
        surface=surface,
        http_status=http_status,
        content_type=content_type,
    )


def assess_media_source_content(
    content: bytes,
    *,
    surface: str,
    media_type: str,
) -> SourceContentAssessment:
    """Confirm that an uploaded audio/video payload has a recognized container signature."""
    normalized_type = str(media_type or "").casefold()
    if not content:
        state = CONTENT_STATE_UNCONFIRMED
        reasons = ("empty_file",)
    elif _matches_media_signature(content, media_type=normalized_type):
        state = CONTENT_STATE_CONFIRMED
        reasons = ()
    else:
        state = CONTENT_STATE_UNAVAILABLE
        reasons = ("invalid_media_signature",)
    return SourceContentAssessment(
        state=state,
        reasons=reasons,
        char_count=len(content),
        non_whitespace_chars=len(content),
        surface=surface,
    )


def content_assessment_metadata(assessment: SourceContentAssessment) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CONTENT_ASSESSMENT_SCHEMA_VERSION,
        "state": assessment.state,
        "reasons": list(assessment.reasons),
        "char_count": assessment.char_count,
        "non_whitespace_chars": assessment.non_whitespace_chars,
        "surface": assessment.surface,
    }
    if assessment.http_status is not None:
        payload["http_status"] = assessment.http_status
    if assessment.content_type:
        payload["content_type"] = assessment.content_type
    return payload


def extract_web_document_text(html: str) -> str:
    """Extract a coverage-first document view without executing untrusted page code.

    Triage needs the complete article, not only the extractor's highest-confidence
    section.  ``favor_precision`` can silently reduce a long page to an appendix or
    another isolated block, so recall is the primary immutable read view.  A
    candidate that is itself an access/error surface is never promoted over a
    healthier fallback extraction.
    """
    value = str(html or "")
    try:
        import trafilatura
    except Exception:
        trafilatura = None

    candidates: list[str] = []
    if trafilatura is not None:
        # Keep the attempts ordered by the product's goal: preserve document
        # coverage first, then fall back to balanced/precision extraction when
        # the recall candidate is empty or recognisably a transport/UI surface.
        for preference in ("recall", "balanced", "precision"):
            options: dict[str, Any] = {
                "output_format": "markdown",
                "include_comments": False,
                "include_tables": True,
                "include_links": False,
            }
            if preference == "recall":
                options["favor_recall"] = True
            elif preference == "precision":
                options["favor_precision"] = True
            try:
                extracted = trafilatura.extract(value, **options)
            except Exception:
                continue
            candidate = str(extracted or "").strip()
            if not candidate or candidate in candidates:
                continue
            candidates.append(candidate)
            assessment = assess_web_source_content(
                candidate,
                surface=f"document_extraction_{preference}",
                content_type="text/markdown",
                input_is_markup=False,
            )
            if assessment.confirmed:
                return candidate

    # Preserve the best non-empty extractor result even when the document is too
    # short for the readiness threshold.  The caller owns readiness; extraction
    # must not discard or truncate semantic content.
    if candidates:
        return max(candidates, key=len)
    return visible_web_text(
        value,
        content_type="text/html",
        input_is_markup=True,
    )


def apply_redirect_target_guard(
    assessment: SourceContentAssessment,
    *,
    requested_url: str | None,
    final_url: str | None,
) -> SourceContentAssessment:
    """Reject only high-confidence redirects to an access/consent endpoint."""
    if not assessment.confirmed or not requested_url or not final_url:
        return assessment
    requested = urlparse(str(requested_url))
    final = urlparse(str(final_url))
    requested_target = (requested.hostname or "", requested.path.rstrip("/") or "/")
    final_target = (final.hostname or "", final.path.rstrip("/") or "/")
    if requested_target == final_target:
        return assessment
    final_segments = {segment.casefold() for segment in final.path.split("/") if segment}
    requested_host = (requested.hostname or "").casefold()
    final_host = (final.hostname or "").casefold()
    final_host_labels = {label for label in final_host.split(".") if label}
    redirected_to_access_host = (
        requested_host != final_host
        and bool(final_host_labels.intersection(_ACCESS_REDIRECT_HOST_LABELS))
    )
    if not final_segments.intersection(_ACCESS_REDIRECT_SEGMENTS) and not redirected_to_access_host:
        return assessment
    return SourceContentAssessment(
        state=CONTENT_STATE_UNAVAILABLE,
        reasons=tuple(dict.fromkeys([*assessment.reasons, "redirect_to_access_page"])),
        char_count=assessment.char_count,
        non_whitespace_chars=assessment.non_whitespace_chars,
        surface=assessment.surface,
        http_status=assessment.http_status,
        content_type=assessment.content_type,
    )


def source_version_content_state(version: Any) -> str:
    metadata = getattr(version, "metadata_", None)
    assessment = metadata.get("content_assessment") if isinstance(metadata, dict) else None
    state = assessment.get("state") if isinstance(assessment, dict) else None
    schema_version = assessment.get("schema_version") if isinstance(assessment, dict) else None
    # V1 remains readable for completed historical runs. Corpus preparation
    # re-evaluates it from the immutable representation before a new agent may
    # use it; newly persisted assessments always use the current schema.
    if state in _CONTENT_STATES and schema_version in _COMPATIBLE_CONTENT_ASSESSMENT_SCHEMA_VERSIONS:
        return str(state)
    if state in {CONTENT_STATE_UNAVAILABLE, CONTENT_STATE_UNCONFIRMED}:
        return str(state)
    if state == CONTENT_STATE_CONFIRMED:
        return CONTENT_STATE_UNCONFIRMED

    # Compatibility for immutable versions created before this additive
    # assessment existed. New capture paths always persist the explicit field.
    status = str(getattr(version, "status", "") or "")
    if status == str(V2RecordStatus.READY):
        return CONTENT_STATE_CONFIRMED
    if status == str(V2RecordStatus.FAILED):
        return CONTENT_STATE_UNAVAILABLE
    return CONTENT_STATE_UNCONFIRMED


def source_version_content_confirmed(version: Any) -> bool:
    return bool(
        str(getattr(version, "status", "") or "") == str(V2RecordStatus.READY)
        and source_version_content_state(version) == CONTENT_STATE_CONFIRMED
    )


def source_version_model_eligible(version: Any) -> bool:
    return bool(
        source_version_content_confirmed(version)
        and getattr(version, "storage_path", None)
        and str(getattr(version, "kind", "") or "") != str(SourceRepresentationKind.ORIGINAL_URL)
    )


def native_ingest_has_confirmed_provider_content(metadata: dict[str, Any] | None) -> bool:
    native = metadata.get("v2_native_ingest") if isinstance(metadata, dict) else None
    if not isinstance(native, dict) or native.get("status") != "provider_ready":
        return False
    try:
        ready_provider_file_count = int(native.get("ready_provider_file_count") or 0)
    except (TypeError, ValueError):
        ready_provider_file_count = 0
    return bool(
        native.get("native_content_ready") is True
        and str(native.get("canonical_source_version_id") or "").strip()
        and ready_provider_file_count > 0
    )


def status_for_content_assessment(assessment: SourceContentAssessment) -> V2RecordStatus:
    return V2RecordStatus.READY if assessment.confirmed else V2RecordStatus.FAILED


def visible_web_text(
    text: str,
    *,
    content_type: str = "",
    input_is_markup: bool | None = None,
) -> str:
    value = str(text or "")
    normalized_type = str(content_type or "").casefold()
    should_parse_markup = (
        input_is_markup
        if input_is_markup is not None
        else any(marker in normalized_type for marker in _MARKUP_CONTENT_TYPES) or bool(_MARKUP_PREFIX_RE.search(value))
    )
    if not should_parse_markup:
        return value
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return value
    extracted = "\n".join(part for part in parser.parts if part.strip())
    return extracted.strip()


def _from_web_health(
    health: WebQualityAssessment,
    *,
    surface: str,
    http_status: int | None,
    content_type: str,
) -> SourceContentAssessment:
    state = {
        "ok": CONTENT_STATE_CONFIRMED,
        "blocked": CONTENT_STATE_UNAVAILABLE,
        "low_signal": CONTENT_STATE_UNCONFIRMED,
    }.get(health.state, CONTENT_STATE_UNCONFIRMED)
    return SourceContentAssessment(
        state=state,
        reasons=tuple(health.reasons),
        char_count=health.char_count,
        non_whitespace_chars=health.non_whitespace_chars,
        surface=surface,
        http_status=http_status,
        content_type=content_type,
    )


def _matches_media_signature(content: bytes, *, media_type: str) -> bool:
    probe = content[:64]
    common_audio = (
        probe.startswith(b"ID3")
        or (len(probe) >= 2 and probe[0] == 0xFF and probe[1] & 0xE0 == 0xE0)
        or probe.startswith(b"fLaC")
        or probe.startswith(b"OggS")
        or (probe.startswith(b"RIFF") and probe[8:12] == b"WAVE")
        or (probe.startswith(b"FORM") and probe[8:12] in {b"AIFF", b"AIFC"})
    )
    common_video = (
        (len(probe) >= 12 and probe[4:8] == b"ftyp")
        or probe.startswith(b"\x1aE\xdf\xa3")
        or probe.startswith(b"\x00\x00\x01\xba")
        or probe.startswith(b"\x00\x00\x01\xb3")
        or (probe.startswith(b"RIFF") and probe[8:12] == b"AVI ")
    )
    if media_type == "audio":
        return common_audio or (len(probe) >= 12 and probe[4:8] == b"ftyp")
    if media_type == "video":
        return common_video
    return common_audio or common_video


class _VisibleTextParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
    _BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag.casefold() in self._BLOCK_TAGS and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif normalized in self._BLOCK_TAGS and self.parts:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


__all__ = [
    "CONTENT_ASSESSMENT_SCHEMA_VERSION",
    "CONTENT_STATE_CONFIRMED",
    "CONTENT_STATE_UNAVAILABLE",
    "CONTENT_STATE_UNCONFIRMED",
    "SourceContentAssessment",
    "assess_pdf_source_content",
    "assess_media_source_content",
    "assess_web_source_content",
    "apply_redirect_target_guard",
    "content_assessment_metadata",
    "extract_web_document_text",
    "native_ingest_has_confirmed_provider_content",
    "source_version_content_confirmed",
    "source_version_content_state",
    "source_version_model_eligible",
    "status_for_content_assessment",
    "visible_web_text",
]
