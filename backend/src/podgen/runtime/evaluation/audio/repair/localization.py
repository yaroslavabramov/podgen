"""Choose a complete original unit from acoustic evidence; never cut audio at these timestamps."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from podgen.domain.errors import ContractError


def bind_fragment(parent, part_index, extraction, decision):
    metrics = parent["result"]["metrics"]
    rate = metrics["sample_rate_hz"]
    if extraction["sample_rate_hz"] != rate:
        raise ContractError("repair_review_timebase_mismatch")
    regions = (
        parent["result"]["frame_map"]
        if parent["kind"] == "assembly"
        else [dict(master_start_frame=0, master_end_frame=metrics["sample_frames"])]
    )
    owners = set()
    ranges = []
    for span in decision.heard_spans:
        first = extraction["start_frame"] + int(
            (Decimal(str(span.start_seconds)) * rate).to_integral_value(rounding=ROUND_FLOOR)
        )
        last = extraction["start_frame"] + int(
            (Decimal(str(span.end_seconds)) * rate).to_integral_value(rounding=ROUND_CEILING)
        )
        if not 0 <= first < last <= metrics["sample_frames"]:
            raise ContractError("repair_heard_evidence_outside_parent")
        overlaps = {
            i
            for i, r in enumerate(regions)
            if first < r["master_end_frame"] and last > r["master_start_frame"]
        }
        owners.update(overlaps)
        ranges.append(dict(start_frame=first, end_frame=last, words=span.words))
    if len(owners) != 1:
        raise ContractError("repair_acoustic_evidence_ambiguous_across_fragments")
    if owners != {part_index}:
        raise ContractError("repair_acoustic_evidence_addresses_different_fragment")
    return dict(
        contract="confirmed-fragment-localization/1",
        part_index=part_index,
        sample_rate_hz=rate,
        heard_master_ranges=ranges,
        part_master_range=dict(
            start_frame=regions[part_index]["master_start_frame"],
            end_frame=regions[part_index]["master_end_frame"],
        ),
        basis="heard_spans_and_exact_part_frame_map",
        frame_rounding="floor_start_ceil_end",
        audio_edit_scope="entire_original_fragment",
    )
