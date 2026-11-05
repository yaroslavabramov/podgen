"""Structural reference rules shared by repository and offline package validation."""

from .contracts.common import ArtifactRef, BlobRef, RuntimeRef
from .errors import ContractError

# Polymorphic basis/context refs deliberately have no nominal restriction.
REF_KINDS = {
    "episode_spec_ref": "EpisodeSpec",
    "policy_ref": "PolicyBundle",
    "source_policy_ref": "Policy",
    "quality_policy_ref": "Policy",
    "audio_policy_ref": "Policy",
    "data_policy_ref": "Policy",
    "operations_policy_ref": "Policy",
    "scoring_policy_ref": "Policy",
    "source_access_policy_ref": "Policy",
    "data_retention_policy_ref": "Policy",
    "usage_policy_ref": "Policy",
    "storage_policy_ref": "Policy",
    "publication_policy_ref": "Policy",
    "dossier_ref": "DossierSnapshot",
    "story_plan_ref": "StoryPlan",
    "script_ref": "ScriptRevision",
    "parent_script_ref": "ScriptRevision",
    "base_script_ref": "ScriptRevision",
    "next_script_ref": "ScriptRevision",
    "checked_against_script_ref": "ScriptRevision",
    "prior_script_ref": "ScriptRevision",
    "source_revision_ref": "SourceRevision",
    "source_revision_refs": "SourceRevision",
    "source_snapshot_refs": "SourceRevision",
    "left_source_ref": "SourceRevision",
    "right_source_ref": "SourceRevision",
    "representation_ref": "SourceRepresentation",
    "derived_from_representation_ref": "SourceRepresentation",
    "evidence_ref": "EvidenceSpan",
    "evidence_refs": "EvidenceSpan",
    "candidate_evidence_refs": "EvidenceSpan",
    "counterevidence_refs": "EvidenceSpan",
    "note_ref": "ResearchNote",
    "note_refs": "ResearchNote",
    "overview_note_ref": "ResearchNote",
    "timeline_note_ref": "ResearchNote",
    "resolution_note_ref": "ResearchNote",
    "known_note_refs": "ResearchNote",
    "answer_note_refs": "ResearchNote",
    "supersedes_note_refs": "ResearchNote",
    "question_ref": "QuestionRecord",
    "question_refs": "QuestionRecord",
    "active_question_refs": "QuestionRecord",
    "uncertainty_refs": "UncertaintyRecord",
    "state_ref": "EditorialState",
    "editorial_state_ref": "EditorialState",
    "editorial_decision_ref": "EditorialDecision",
    "decision_ref": "EditorialDecision",
    "current_decision_refs": "EditorialDecision",
    "request_ref": "ResearchRequest",
    "research_request_ref": "ResearchRequest",
    "unresolved_research_request_refs": "ResearchRequest",
    "open_research_request_refs": "ResearchRequest",
    "claim_set_ref": "ClaimSet",
    "verification_report_ref": "VerificationReport",
    "reused_from_report_ref": "VerificationReport",
    "editorial_review_ref": "EditorialReview",
    "listener_state_ref": "ListenerState",
    "calculation_refs": "CalculationRecord",
    "family_assessment_refs": "SourceFamilyAssessment",
    "source_family_assessment_refs": "SourceFamilyAssessment",
    "relationship_refs": "SourceRelation",
    "performance_plan_ref": "PerformancePlan",
    "audio_master_ref": "AudioMaster",
    "audio_segment_ref": "AudioSegment",
    "segment_refs": "AudioSegment",
    "audio_qa_ref": "AudioQAReport",
    "transcript_ref": "Transcript",
    "show_notes_ref": "ShowNotes",
    "show_notes_review_ref": "ShowNotesReview",
    "voice_profile_ref": "VoiceProfile",
    "voice_profile_refs": "VoiceProfile",
    "lexicon_ref": "PronunciationLexicon",
    "model_profile_ref": "ModelProfile",
    "fallback_profile_refs": "ModelProfile",
    "authorized_source_refs": "SourceRevision",
    "model_profile_refs": "ModelProfile",
    "prompt_ref": "PromptDefinition",
    "toolset_ref": "ToolsetDefinition",
    "render_profile_ref": "ProcessingProfile",
    "delivery_profile_ref": "ProcessingProfile",
    "assembly_profile_ref": "ProcessingProfile",
    "recognition_profile_refs": "ProcessingProfile",
    "alignment_profile_ref": "ProcessingProfile",
    "decoder_profile_ref": "ProcessingProfile",
    "resample_profile_ref": "ProcessingProfile",
    "execution_profile_ref": "ProcessingProfile",
    "extraction_profile_ref": "ProcessingProfile",
    "extractor_profile_ref": "ModelProfile",
    "evaluation_case_ref": "EvaluationCase",
    "case_ref": "EvaluationCase",
    "reference_annotation_ref": "EvaluationAnnotation",
    "human_assessment_refs": "HumanAssessment",
}
POLICY_SUBTYPES = {
    "source_policy_ref": "source",
    "source_access_policy_ref": "source",
    "quality_policy_ref": "quality",
    "audio_policy_ref": "audio",
    "data_policy_ref": "data",
    "operations_policy_ref": "operations",
    "scoring_policy_ref": "evaluation",
    "data_retention_policy_ref": "data",
}
PROCESSING_SUBTYPES = {
    "render_profile_ref": "render",
    "delivery_profile_ref": "delivery",
    "assembly_profile_ref": "assembly",
    "alignment_profile_ref": "alignment",
    "decoder_profile_ref": "media_decode",
    "resample_profile_ref": "media_decode",
    "execution_profile_ref": "calculation",
    "recognition_profile_refs": "recognition",
}
LINEAGE_FIELDS = {"parent_script_ref", "supersedes_note_refs", "base_script_ref", "prior_input_ref"}


def walk_values(value, field="", path=""):
    """Yield (field, JSON path, typed ref); never resolve diagnostic aliases."""
    if isinstance(value, dict):
        if set(value) == {"artifact_id", "revision_id", "kind"}:
            ref = ArtifactRef.model_validate(value)
            expected = REF_KINDS.get(field)
            if expected and ref.kind != expected:
                raise ContractError("reference_kind_mismatch", f"{path}: expected {expected}")
            yield field, path, ref
        elif set(value) == {"storage_key", "sha256", "byte_length", "media_type"}:
            yield field, path, BlobRef.model_validate(value)
        elif set(value) == {"record_kind", "record_id"}:
            yield field, path, RuntimeRef.model_validate(value)
        else:
            for key, child in value.items():
                yield from walk_values(child, key, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_values(child, field, f"{path}/{index}")


def check_subtype(field, payload):
    if field in POLICY_SUBTYPES and payload.get("policy_kind") != POLICY_SUBTYPES[field]:
        raise ContractError("policy_subtype_mismatch", field)
    if field in PROCESSING_SUBTYPES and payload.get("profile_kind") != PROCESSING_SUBTYPES[field]:
        raise ContractError("processing_subtype_mismatch", field)
