"""Compile contiguous source ranges without losing speaker or sentence identity."""

from uuid import uuid5

from podgen.audio.editing.timeline import sentences
from podgen.audio.fidelity.tokens import expected_tokens
from podgen.audio.synthesis.contracts import SynthesisInput, SynthesisUtterance
from podgen.domain.errors import ContractError


def identity(row):
    return tuple(row[k] for k in ("utterance_id", "utterance_index", "char_start", "char_end"))


def compile_context(proposal_id, source, selectors, output_language):
    rows = sentences(source["utterances"])
    positions = {identity(row): i for i, row in enumerate(rows)}
    try:
        selected = [positions[identity(row)] for row in selectors]
    except KeyError as error:
        raise ContractError("local_replacement_source_selector_missing") from error
    if any(
        rows[position][field] != selector[field]
        for position, selector in zip(selected, selectors, strict=True)
        for field in ("text", "speaker")
    ):
        raise ContractError("local_replacement_source_selector_content_changed")
    if selected != list(range(selected[0], selected[-1] + 1)):
        raise ContractError("local_replacement_noncontiguous_source_selection")
    first, last = max(0, selected[0] - 1), min(len(rows), selected[-1] + 2)
    ranges, utterances, target_tokens = [], [], []
    for row in rows[first:last]:
        if ranges and ranges[-1]["utterance_index"] == row["utterance_index"]:
            ranges[-1]["char_end"] = row["char_end"]
        else:
            ranges.append(dict(row))
    for i, row in enumerate(ranges):
        original = source["utterances"][row["utterance_index"]]
        row["text"] = original["text"][row["char_start"] : row["char_end"]]
        utterances.append(
            SynthesisUtterance(
                utterance_id=uuid5(proposal_id, "context:" + str(i)),
                speaker=original["speaker"],
                text=row["text"],
            )
        )
    short = SynthesisInput(
        utterances=utterances,
        direction=source["direction"],
        output_language=output_language,
        speaker_voices={
            key: value
            for key, value in source.get("speaker_voices", {}).items()
            if key in {row.speaker for row in utterances}
        },
    )
    for token in expected_tokens(short.model_dump(mode="json")["utterances"]):
        row = ranges[token["utterance_index"]]
        if any(
            selector["utterance_index"] == row["utterance_index"]
            and selector["char_start"] <= row["char_start"] + token["char_start"]
            and row["char_start"] + token["char_end"] <= selector["char_end"]
            for selector in selectors
        ):
            target_tokens.append(token["index"])
    if not target_tokens or target_tokens != list(range(target_tokens[0], target_tokens[-1] + 1)):
        raise ContractError("local_replacement_invalid_target_token_range")
    return short, dict(
        source_ranges=ranges,
        sentence_ranges=rows[first:last],
        target_ranges=selectors,
        target_token_range=[target_tokens[0], target_tokens[-1] + 1],
        context_policy="one_available_sentence_each_side_grouped_by_original_utterance",
    )
