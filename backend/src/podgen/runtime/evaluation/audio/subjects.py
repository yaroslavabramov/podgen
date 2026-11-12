from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from .attempts import history


def comparison_result(journal, result_ref):
    manifest = journal.get("manifest")
    if manifest is None:
        raise ContractError("fidelity_comparison_not_prepared")
    for cell in manifest["cells"]:
        for row in history(journal, cell):
            for revision in row["processing"]:
                if revision["result"] and journal.ref(revision["key"] + ":result") == result_ref:
                    result = journal.base.read(result_ref)
                    decoded = journal.base.read(RuntimeRef.model_validate(result["decoded_ref"]))
                    source = journal.base.read(RuntimeRef.model_validate(cell["source_ref"]))
                    if (
                        decoded["pcm_blob"] != result["decoded_pcm_blob"]
                        or not decoded["metrics"]["full_decode"]
                    ):
                        raise ContractError("fidelity_subject_decoded_binding_mismatch")
                    if len({u["speaker"] for u in source["source"]["utterances"]}) != 1:
                        raise ContractError("fidelity_requires_one_narrator")
                    return dict(
                        result=result,
                        source=source,
                        source_ref=cell["source_ref"],
                        decoded=decoded,
                        kind="comparison_result",
                        audio_ref=result_ref.model_dump(mode="json"),
                        audio_blob=result["decoded_pcm_blob"],
                        decoded_ref=result["decoded_ref"],
                        audio_variant="decoded_pcm_before_playback_gain",
                        playback_audio_blob=result["blind_audio_blob"],
                        playback_normalization=result["normalization"],
                        profile=row["prepared"]["profile"],
                        original_request=row["prepared"]["request"],
                        original_attempt_key=row["key"],
                        original_wire_blob=row["prepared"]["wire_request_blob"],
                        wire_authority_key=row["key"] + ":prepared",
                        repair_depth=0,
                        origin_part_ref=result_ref.model_dump(mode="json"),
                    )
    raise ContractError("fidelity_result_outside_comparison")


def synthesis_part(journal, binding):
    if binding.kind == "repair_part":
        from .repair.subject import resolve

        return resolve(journal, binding.ref)
    return comparison_result(journal, binding.ref)


def audio_subject(journal, binding):
    if binding.kind == "assembly":
        from .assembly.subject import resolve

        return resolve(journal, binding.ref)
    return comparison_result(journal, binding.ref)
