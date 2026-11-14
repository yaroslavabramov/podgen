"""Bind a duration estimate to exact saved assembly metadata, without reading WAV bytes."""

from typing import Literal

from pydantic import Field

from podgen.audio.synthesis.contracts import SparseDirection, SynthesisProfile
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import Contract, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.duration_reference import VoiceDurationEstimate
from podgen.writing.roles import WriteOutput


class SavedDurationReference(Contract):
    contract: Literal["saved-voice-duration-reference/1"] = "saved-voice-duration-reference/1"
    assembly_ref: RuntimeRef
    source_script_ref: RuntimeRef
    source_script_hash: str = Field(pattern="^[0-9a-f]{64}$")
    estimate: VoiceDurationEstimate

    def require_voice(self, synthesis, direction):
        if self.estimate.synthesis != synthesis or self.estimate.direction != direction:
            raise ContractError("duration_reference_voice_or_direction_mismatch")


def resolve(repo, project, assembly_ref, synthesis, direction=None):
    """Read three immutable JSON records; live repository authorization stays intact."""
    direction = direction if direction is not None else SparseDirection()
    if assembly_ref.record_kind != "tool_execution":
        raise ContractError("duration_reference_requires_assembly_result")
    result = repo.get_runtime(project, assembly_ref)
    if result.get("contract") != "audio-evaluation-assembly-result/1":
        raise ContractError("duration_reference_requires_assembly_result")
    script_ref = RuntimeRef.model_validate(result["source_script_ref"])
    manifest, script_record = repo.get_runtime_many(
        project, [RuntimeRef.model_validate(result["manifest_ref"]), script_ref]
    )
    if manifest.get("contract") != "audio-evaluation-assembly-manifest/1":
        raise ContractError("duration_reference_manifest_invalid")
    for field in ("configuration_hash", "source_script_ref", "source_script_hash", "source_ref", "coverage"):
        if result.get(field) != manifest.get(field):
            raise ContractError("duration_reference_assembly_binding_mismatch")
    if SynthesisProfile.model_validate(manifest["profile"]["synthesis"]) != synthesis or (
        SparseDirection.model_validate(manifest["direction"]) != direction
    ):
        raise ContractError("duration_reference_voice_or_direction_mismatch")
    coverage = result["coverage"]
    if (
        coverage.get("full_script") is not True
        or coverage.get("duplicates") != []
        or coverage.get("gaps") != []
    ):
        raise ContractError("duration_reference_requires_complete_coverage")
    script = script_record["output"]
    WriteOutput.model_validate(script)
    if digest(script) != result["source_script_hash"] or len(script["utterances"]) != coverage.get(
        "utterances"
    ):
        raise ContractError("duration_reference_script_binding_mismatch")
    if len({utterance["speaker"] for utterance in script["utterances"]}) != 1:
        raise ContractError("duration_reference_requires_solo_recording")
    metrics = result["metrics"]
    if metrics.get("full_decode") is not True:
        raise ContractError("duration_reference_requires_measured_audio")
    return SavedDurationReference(
        assembly_ref=assembly_ref,
        source_script_ref=script_ref,
        source_script_hash=result["source_script_hash"],
        estimate=VoiceDurationEstimate(
            synthesis=synthesis,
            direction=direction,
            script_word_count=sum(len(utterance["text"].split()) for utterance in script["utterances"]),
            sample_frames=metrics["sample_frames"],
            sample_rate_hz=metrics["sample_rate_hz"],
        ),
    )


def validate(repo, project, reference, synthesis):
    reference.require_voice(synthesis, SparseDirection())
    expected = resolve(repo, project, reference.assembly_ref, synthesis)
    if reference != expected:
        raise ContractError("duration_reference_measurement_changed")
