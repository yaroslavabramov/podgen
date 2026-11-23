"""Saved-source acquisition contracts, independent of research providers."""

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from podgen.domain.contracts.common import ArtifactRef, Contract, RuntimeRef, Text
from podgen.domain.contracts.evidence import SourceRights

VERSION = "saved-source-acquisition/1"


class AcquisitionProfile(Contract):
    max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    timeout_seconds: float = Field(default=30, gt=0, le=1800)
    max_redirects: int = Field(default=5, ge=0, le=20)
    # A reader's browser identity; the bot label was refused by ordinary article pages.
    user_agent: Text = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    accept_language: Text = "en,ru;q=0.8"
    min_text_chars: int = Field(default=100, ge=1)
    pdf_mode: Literal["marker_with_text_fallback", "text_layer_only"] = "marker_with_text_fallback"
    marker_python_executable: Text | None = None
    marker_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    enable_jina: bool = False
    jina_credential_env: Text = "JINA_API_KEY"
    enable_browser: bool = True
    prefer_mediawiki: bool = True


class AcquisitionRequest(Contract):
    contract: Literal["source-acquisition-request/1"] = "source-acquisition-request/1"
    operation_id: UUID
    source_id: UUID
    url: Text
    profile_ref: ArtifactRef
    rights: SourceRights
    language: Text = "und"


class ReExtractionRequest(Contract):
    contract: Literal["source-reextraction-request/1"] = "source-reextraction-request/1"
    operation_id: UUID
    source_revision_ref: ArtifactRef
    profile_ref: ArtifactRef
    reason: Text
    language: Text | None = None


class AcquiredSource(Contract):
    source_revision_ref: ArtifactRef
    representation_refs: list[ArtifactRef]
    role: Literal["original", "mediawiki", "jina", "promoted_pdf", "browser"]
    acquisition_record_ref: RuntimeRef
    status: Literal["available", "unavailable", "unconfirmed", "extraction_failed"]
    diagnostics: list[str]


class AcquisitionResult(Contract):
    contract: Literal["source-acquisition-result/1"] = "source-acquisition-result/1"
    acquisition_record_ref: RuntimeRef
    sources: list[AcquiredSource]
    status: Literal["available", "unavailable", "unconfirmed", "failed"]
    diagnostics: list[str]


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    resolved_url: str
    redirect_chain: list[dict]
    status_code: int
    headers: dict[str, str]
    body: bytes
    acquired_at: str
    acquisition_kind: str = "http"
    limitations: list[str] = field(default_factory=list)


class SourceFetcher(Protocol):
    async def fetch(self, url: str, profile: AcquisitionProfile, *, headers=None) -> FetchedDocument: ...
