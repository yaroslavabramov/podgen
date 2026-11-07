"""Only explicitly authorized sequence boundaries permit replacing complete utterances."""


def sequence_catalog(script, readings):
    declared = []
    for origin in ("reader", "fact"):
        scope = readings[origin]["revision_scope"]
        if scope["scale"] != "sequence":
            continue
        for index, sequence in enumerate(scope["sequence_ranges"]):
            first, last = sequence["anchors"]
            start, end = first["utterance_index"], last["utterance_index"]
            if not 0 <= start <= end < len(script["utterances"]):
                raise ValueError("revision_sequence_boundaries_invalid")
            for anchor in (first, last):
                text = script["utterances"][anchor["utterance_index"]]["text"]
                quote = anchor["quote"]
                location = text.find(quote)
                if not quote or location < 0:
                    raise ValueError("revision_sequence_quote_mismatch")
                if text.find(quote, location + 1) >= 0:
                    raise ValueError("revision_sequence_quote_ambiguous")
            declared.append(
                dict(
                    start=start,
                    end=end + 1,
                    declarations=[
                        dict(
                            origin=origin, index=index, anchors=sequence["anchors"], reason=sequence["reason"]
                        )
                    ],
                )
            )
    merged = []
    for row in sorted(declared, key=lambda r: (r["start"], r["end"])):
        if merged and row["start"] < merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], row["end"])
            merged[-1]["declarations"].extend(row["declarations"])
        else:
            merged.append(row)
    return [dict(range_id=f"R{i}", **row) for i, row in enumerate(merged, 1)]


def replacements(script, edits, catalog):
    offered = {row["range_id"]: row for row in catalog}
    result = {}
    for edit in edits:
        target = offered.get(edit.range_id)
        if target is None:
            raise ValueError("revision_sequence_not_offered")
        start, end = target["start"], target["end"]
        if start in result:
            raise ValueError("revision_sequences_overlap")
        utterances = [row.model_dump(mode="json") for row in edit.utterances]
        if utterances == script["utterances"][start:end]:
            raise ValueError("revision_sequence_is_noop")
        result[start] = (end, utterances)
    return result
