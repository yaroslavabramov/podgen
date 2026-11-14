"""Import exact evaluation-approved speech with both final reviews and stable source IDs."""

from uuid import UUID, uuid5

from podgen.audio.synthesis.contracts import SynthesisInput, SynthesisUtterance
from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from ..reading_cycle import outcome_ref
from ..reading_validation import read_descriptor


def import_fragment(journal, fragment, cache):
    descriptor = cache.get(fragment.outcome_ref)
    outcome = descriptor.outcome if descriptor is not None else journal.base.read(fragment.outcome_ref)
    if outcome.get("contract") != "independent-reading-outcome/1":
        raise ContractError("audio_source_is_not_native_outcome")
    run_id = UUID(outcome["run_id"])
    if fragment.outcome_ref != outcome_ref(run_id):
        raise ContractError("audio_source_is_not_native_outcome")
    if descriptor is None:
        descriptor = read_descriptor(journal.base, run_id, outcome=outcome)
        cache[fragment.outcome_ref] = descriptor
    if descriptor is None or descriptor.outcome != outcome or outcome["status"] != "keep":
        raise ContractError("audio_source_not_evaluation_approved")
    descriptor.authorize(journal.base)
    script = outcome["output"]
    if digest(script) != outcome["script_hash"]:
        raise ContractError("audio_source_script_hash_mismatch")
    utterances, selectors = [], []
    previous = (-1, -1)
    script_id = UUID(outcome["script_ref"]["record_id"])
    for selection in fragment.selections:
        index = selection.utterance_index
        if index >= len(script["utterances"]):
            raise ContractError("audio_fragment_utterance_outside_script")
        row = script["utterances"][index]
        start, end = selection.start_char, selection.end_char or len(row["text"])
        if not 0 <= start < end <= len(row["text"]) or (index, start) < previous:
            raise ContractError("audio_fragment_ranges_invalid_or_overlapping")
        previous = (index, end)
        uid = uuid5(script_id, f"utterance:{index}")
        utterances.append(
            SynthesisUtterance(utterance_id=uid, speaker=row["speaker"], text=row["text"][start:end])
        )
        selectors.append(dict(utterance_id=str(uid), utterance_index=index, char_start=start, char_end=end))
    source = SynthesisInput(utterances=utterances, direction=fragment.direction)
    return dict(
        contract="evaluation-audio-source/1",
        purpose="evaluation",
        semantic_acceptance=False,
        production_ready=False,
        outcome_ref=fragment.outcome_ref.model_dump(mode="json"),
        source_script_ref=outcome["script_ref"],
        source_script_hash=outcome["script_hash"],
        reading_refs=outcome["reading_refs"],
        selectors=selectors,
        source=source.model_dump(mode="json"),
    )
