from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from .contracts import prefix


def resolve(journal, ref):
    result = journal.base.read(ref)
    if result.get("contract") != "audio-evaluation-assembly-result/1":
        raise ContractError("fidelity_subject_is_not_assembly")
    try:
        key = prefix(UUID(result["assembly_id"]))
    except (ValueError, KeyError) as error:
        raise ContractError("fidelity_invalid_assembly_identity") from error
    manifest = journal.get(key + ":manifest")
    if (
        not manifest
        or journal.ref(key + ":result") != ref
        or journal.get(key + ":result") != result
        or result["comparison_run_id"] != str(journal.run_id)
        or result["manifest_ref"] != journal.ref(key + ":manifest").model_dump(mode="json")
        or result["source_ref"] != manifest["source_ref"]
        or result["coverage"] != manifest["coverage"]
        or not result["coverage"]["full_script"]
        or result["configuration_hash"] != manifest["configuration_hash"]
    ):
        raise ContractError("fidelity_assembly_outside_comparison")
    source = journal.base.read(RuntimeRef.model_validate(result["source_ref"]))
    if (
        source["source_script_hash"] != result["source_script_hash"]
        or source["source_script_ref"] != result["source_script_ref"]
    ):
        raise ContractError("fidelity_assembly_source_mismatch")
    return dict(
        kind="assembly",
        audio_ref=ref.model_dump(mode="json"),
        audio_blob=result["master_pcm_blob"],
        decoded_ref=None,
        audio_variant="assembled_final_playback_pcm",
        playback_audio_blob=result["playback_audio_blob"],
        playback_normalization=result["normalization"],
        source_ref=result["source_ref"],
        source=source,
        profile=manifest["profile"],
        result=result,
    )
