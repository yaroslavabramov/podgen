from __future__ import annotations

from enum import StrEnum


class CorpusProvider(StrEnum):
    OPENAI = "openai"


class ProviderUploadPhase(StrEnum):
    DB_PREPARED = "db_prepared"
    FILE_UPLOADED = "file_uploaded"
    ATTACHED = "attached"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    ADOPTED = "adopted"
    DETACHED = "detached"


class OpenAIProviderFileStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FILE_UPLOADED = "file_uploaded"
    ATTACHED = "attached"
    RETRIEVE_UNAVAILABLE = "retrieve_unavailable"
    IN_PROGRESS = "in_progress"


class OpenAIUrlReadStatus(StrEnum):
    READABLE = "readable"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class OpenAIUrlRecoveryStatus(StrEnum):
    SAME_URL_READABLE = "same_url_readable"
    ALTERNATE_FOUND = "alternate_found"
    PAYWALLED_OR_BLOCKED = "paywalled_or_blocked"
    GONE = "gone"
    NOT_FOUND = "not_found"
    UNCLEAR = "unclear"
    ERROR = "error"


PROVIDER_TERMINAL_STATUSES = frozenset(
    {
        OpenAIProviderFileStatus.COMPLETED,
        OpenAIProviderFileStatus.FAILED,
        OpenAIProviderFileStatus.CANCELLED,
    }
)

PROVIDER_FAILED_TERMINAL_STATUSES = frozenset(
    {
        OpenAIProviderFileStatus.FAILED,
        OpenAIProviderFileStatus.CANCELLED,
    }
)

PROVIDER_READY_PHASES = frozenset({ProviderUploadPhase.ADOPTED, ProviderUploadPhase.READY})

PROVIDER_INTERNAL_ATTRIBUTE_KEYS = frozenset(
    {
        "upload_phase",
        "last_provider_status",
        "last_reconciled_at",
        "adoption_source",
        "last_openai_error",
        "active_index_status",
        "excluded_reason",
    }
)

OPENAI_URL_READ_SUCCESS_STATUSES = frozenset({OpenAIUrlReadStatus.READABLE, OpenAIUrlReadStatus.PARTIAL})

OPENAI_URL_READ_SCHEMA_ENUM = [status.value for status in OpenAIUrlReadStatus]
OPENAI_URL_RECOVERY_SCHEMA_ENUM = [
    status.value for status in OpenAIUrlRecoveryStatus if status != OpenAIUrlRecoveryStatus.ERROR
]
