from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from .history_snapshot import load


def comparison_result(journal, result_ref):
    return comparison_results(journal, [result_ref])[0]


def comparison_results(journal, result_refs, *, samples=None):
    """Resolve known exact addresses; load legacy ownership once when not supplied."""
    with journal.read_operation():
        return _comparison_results(journal, result_refs, samples=samples)


def _comparison_results(journal, result_refs, *, samples):
    manifest = journal.get("manifest")
    if manifest is None:
        raise ContractError("fidelity_comparison_not_prepared")
    cells = {cell["cell_id"]: cell for cell in manifest["cells"]}
    candidates = {}
    if samples is not None:
        for sample in samples:
            row = sample.baseline
            if row is not None:
                reference = journal.ref(row["processing_key"] + ":result")
                if reference.model_dump(mode="json") != sample.result_ref or sample.cell_id not in cells:
                    raise ContractError("audio_result_descriptor_binding_mismatch")
                candidates[reference] = cells[sample.cell_id], row["key"]
    else:
        for identity, rows in load(journal, manifest["cells"]).items():
            for row in rows:
                for revision in row["processing"]:
                    if revision["result"]:
                        candidates[journal.ref(revision["key"] + ":result")] = cells[identity], row["key"]
    if any(ref not in candidates for ref in result_refs):
        raise ContractError("fidelity_result_outside_comparison")
    # Authorization and bodies belong to this new assembly/public-read boundary.
    # Descriptors only save rediscovering immutable ownership in paid history.
    with journal.read_operation():
        result_values = _read_many(journal, list(result_refs))
        more = []
        for ref, result in zip(result_refs, result_values, strict=True):
            cell, key = candidates[ref]
            more.extend(
                [
                    RuntimeRef.model_validate(result["decoded_ref"]),
                    RuntimeRef.model_validate(cell["source_ref"]),
                    journal.ref(key + ":prepared"),
                ]
            )
        bound = _read_many(journal, more)
    parts = []
    for index, (ref, result) in enumerate(zip(result_refs, result_values, strict=True)):
        cell, key = candidates[ref]
        decoded, source, prepared = bound[index * 3 : index * 3 + 3]
        if (
            decoded["pcm_blob"] != result["decoded_pcm_blob"]
            or not decoded["metrics"]["full_decode"]
            or result["cell_id"] != cell["cell_id"]
            or prepared["cell_id"] != cell["cell_id"]
            or result["take"] != prepared["take"]
            or prepared["source_ref"] != cell["source_ref"]
        ):
            raise ContractError("fidelity_subject_decoded_binding_mismatch")
        if len({u["speaker"] for u in source["source"]["utterances"]}) != 1:
            raise ContractError("fidelity_requires_one_narrator")
        parts.append(
            dict(
                result=result,
                source=source,
                source_ref=cell["source_ref"],
                decoded=decoded,
                kind="comparison_result",
                audio_ref=ref.model_dump(mode="json"),
                audio_blob=result["decoded_pcm_blob"],
                decoded_ref=result["decoded_ref"],
                audio_variant="decoded_pcm_before_playback_gain",
                playback_audio_blob=result["blind_audio_blob"],
                playback_normalization=result["normalization"],
                profile=prepared["profile"],
                original_request=prepared["request"],
                original_attempt_key=key,
                original_wire_blob=prepared["wire_request_blob"],
                wire_authority_key=key + ":prepared",
                repair_depth=0,
                origin_part_ref=ref.model_dump(mode="json"),
            )
        )
    return parts


def _read_many(journal, references):
    values = []
    for offset in range(0, len(references), 64):
        values.extend(journal.base.read_many(references[offset : offset + 64]))
    return values


def synthesis_part(journal, binding):
    if binding.kind == "repair_part":
        from .assembly.legacy_repair import resolve

        return resolve(journal, binding.ref)
    return comparison_result(journal, binding.ref)


def audio_subject(journal, binding):
    if binding.kind == "local_master":
        from .local_replacement.subject import resolve

        return resolve(journal, binding.ref)
    if binding.kind == "assembly":
        from .assembly.subject import resolve

        return resolve(journal, binding.ref)
    return comparison_result(journal, binding.ref)
