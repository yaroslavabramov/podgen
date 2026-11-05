from typing import Literal

from pydantic import JsonValue, model_validator

from .common import (
    UUID,
    ArtifactKind,
    ArtifactRef,
    BlobRef,
    Contract,
    DependencyDisposition,
    Failure,
    NonNegativeInt,
    PositiveInt,
    ProducerRef,
    RuntimeRef,
    Text,
    Timestamp,
)
from .writing import SecondsRange


class SourceScope(Contract):
    mode: Literal["open_web", "supplied_only"]
    source_policy_ref: ArtifactRef
    authorized_input_ids: list[UUID]
    authorized_source_refs: list[ArtifactRef]
    network_rule_id: str | None
    external_processing_rule_id: Text


class InputManifest(Contract):
    manifest_id: UUID
    parent_manifest_id: UUID | None
    project_id: UUID
    task_id: UUID
    revision_refs: list[ArtifactRef]
    runtime_refs: list[RuntimeRef]
    source_scope: SourceScope
    policy_ref: ArtifactRef
    prompt_ref: ArtifactRef
    toolset_ref: ArtifactRef
    model_profile_ref: ArtifactRef


class OperationalLimits(Contract):
    max_model_calls: PositiveInt
    max_tool_calls: PositiveInt
    max_elapsed_seconds: PositiveInt


class TaskSpec(Contract):
    task_id: UUID
    role: Text
    objective: Text
    why_it_matters: Text
    input_manifest_id: UUID
    acceptance_questions: list[str]
    allowed_actions: list[str]
    output_kind: ArtifactKind
    unresolved_context: list[str]
    operational_limits: OperationalLimits


class TaskResult(Contract):
    task_id: UUID
    status: Literal["answered", "partial", "needs_material", "unanswerable", "failed"]
    output_refs: list[ArtifactRef]
    remaining_questions: list[str]
    next_action: str | None
    failure: Failure | None

    @model_validator(mode="after")
    def failed(self):
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("failure_status_mismatch")
        return self


class TaskResultRecord(Contract):
    result_id: UUID
    run_id: UUID
    payload: TaskResult


class QualificationCell(Contract):
    roles: list[str]
    language: Text
    format: Literal["solo", "dialogue"]
    duration_interval: SecondsRange
    companion_profile_refs: list[ArtifactRef]


class QualificationScope(Contract):
    supported_cells: list[QualificationCell]


class FindingRef(Contract):
    report_ref: ArtifactRef
    finding_id: UUID


class AcceptanceRecord(Contract):
    record_id: UUID
    subject_ref: ArtifactRef
    check_kind: Literal["factual", "editorial", "audio", "show_notes", "profile_qualification"]
    decision: Literal["accepted", "changes_required", "rejected"]
    policy_ref: ArtifactRef
    reviewed_input_refs: list[ArtifactRef]
    assessment_scope: QualificationScope | None
    findings: list[FindingRef]
    reviewer_ref: ProducerRef
    created_at: Timestamp

    @model_validator(mode="after")
    def scope(self):
        if (self.check_kind == "profile_qualification") != (self.assessment_scope is not None):
            raise ValueError("qualification_scope_mismatch")
        return self


class RevocationRecord(Contract):
    revocation_id: UUID
    acceptance_record_id: UUID
    reason_code: Text
    basis_refs: list[ArtifactRef]
    reviewer_ref: ProducerRef
    created_at: Timestamp


class RevalidationAuthority(Contract):
    kind: Literal["acceptance_record", "integration_event"]
    record_id: UUID


class DependencyRevalidationEvent(Contract):
    event_id: UUID
    subject_ref: ArtifactRef
    assessment_ref: ArtifactRef
    authority: RevalidationAuthority
    dispositions: list[DependencyDisposition]
    expected_validity_version: NonNegativeInt
    policy_ref: ArtifactRef
    created_at: Timestamp


class PublishedAsset(Contract):
    blob_ref: BlobRef
    media_metadata: dict[str, JsonValue]
    allowed_roles: list[Literal["director", "evaluator"]]


class CatalogPublication(Contract):
    publication_id: UUID
    config_ref: ArtifactRef
    published_assets: list[PublishedAsset]
    allowed_consumer_project_ids: list[UUID]
    publisher_id: UUID
    created_at: Timestamp


class RejectedCandidate(Contract):
    locator: Text
    reason_code: Text


class InputAdmissionRecord(Contract):
    admission_id: UUID
    project_id: UUID
    task_run_id: UUID
    parent_manifest_id: UUID
    next_manifest_id: UUID
    admitted_refs: list[ArtifactRef]
    admitted_runtime_refs: list[RuntimeRef]
    rejected_candidates: list[RejectedCandidate]
    origin: Literal["tool_result", "editor_handoff", "user_input"]
    origin_id: UUID
    policy_ref: ArtifactRef
    scope_decision: Text
    created_at: Timestamp


class ReadReceipt(Contract):
    receipt_id: UUID
    project_id: UUID
    task_run_id: UUID
    invocation_id: UUID
    tool_call_id: UUID | None
    manifest_id: UUID
    artifact_ref: ArtifactRef
    representation_ref: ArtifactRef | None
    preview_ref: ArtifactRef | None
    derived_from_ref: ArtifactRef | None
    requested_selector: dict[str, JsonValue]
    returned_selectors: list[dict[str, JsonValue]]
    display_kind: Literal["preview", "exact_text", "rendered_page", "audio"]
    returned_content_hash: Text
    has_more: bool
    continuation: dict[str, JsonValue] | None


class EpisodeRun(Contract):
    episode_run_id: UUID
    project_id: UUID
    episode_id: UUID
    episode_spec_ref: ArtifactRef
    purpose: Literal["production", "evaluation"]
    evaluation_case_ref: ArtifactRef | None
    generation_epoch: NonNegativeInt
    status: Literal[
        "queued",
        "running",
        "waiting",
        "needs_attention",
        "cancel_requested",
        "cancelled",
        "succeeded",
        "failed",
    ]
    phase: (
        Literal[
            "orienting",
            "researching",
            "planning",
            "drafting",
            "editing",
            "verifying",
            "producing_audio",
            "ready",
        ]
        | None
    )
    editorial_state_ref: ArtifactRef
    waiting_reason: str | None
    failure: Failure | None
    checkpoint_refs: list[ArtifactRef]
    result_release_ref: ArtifactRef | None
    created_at: Timestamp
    started_at: Timestamp | None
    ended_at: Timestamp | None


class TaskRun(Contract):
    run_id: UUID
    task_id: UUID
    attempt: PositiveInt
    episode_run_id: UUID
    purpose: Literal["production", "evaluation"]
    generation_epoch: NonNegativeInt
    current_manifest_id: UUID
    checkpoints: list[ArtifactRef]
    status: Literal["queued", "running", "waiting", "cancel_requested", "cancelled", "succeeded", "failed"]
