"""Short IDs identify exact quoted ranges of one server-bound full script."""

from ..roles import WriteOutput
from .buffers import deletion_buffer, visible_range
from .contexts import source_contexts


def _anchors(readings):
    for origin in ("reader", "fact"):
        review = readings[origin]
        for index, finding in enumerate(review["findings"]):
            yield finding["anchors"], f"{origin}.findings.{index}"
        scope = review["revision_scope"]
        if scope["scale"] != "none":
            for index, dependency in enumerate(scope.get("dependent_passages", [])):
                yield dependency["anchors"], f"{origin}.dependent_passages.{index}"


def target_catalog(script, readings, *, require_targets=True):
    ranges = {}
    WriteOutput.model_validate(script)
    for anchors, basis in _anchors(readings):
        for anchor in anchors:
            index, quote = anchor["utterance_index"], anchor["quote"]
            if not 0 <= index < len(script["utterances"]) or not quote:
                raise ValueError("revision_target_anchor_invalid")
            text = script["utterances"][index]["text"]
            start = text.find(quote)
            if start < 0:
                raise ValueError("revision_target_quote_mismatch")
            if text.find(quote, start + 1) >= 0:
                raise ValueError("revision_target_quote_ambiguous")
            end = start + len(quote)
            offered = [(start, end, basis)]
            contexts = [(first, last) for first, last in source_contexts(text) if first <= start and end <= last]
            if contexts:
                first, last = min(contexts, key=lambda span: span[1] - span[0])
                offered.append((first, last, basis + ".context"))
            for first, last, permission in offered:
                first, last = visible_range(text, first, last)
                visible = text[first:last]
                if text.find(visible) != first or text.find(visible, first + 1) >= 0:
                    raise ValueError("revision_target_quote_ambiguous")
                key = (index, first, last)
                ranges.setdefault(key, [])
                if permission not in ranges[key]:
                    ranges[key].append(permission)
    catalog, boundaries = [], {}
    for number, ((index, start, end), basis) in enumerate(sorted(ranges.items()), 1):
        row = script["utterances"][index]
        catalog.append(
            dict(
                target_id=f"T{number}",
                kind="text",
                utterance_index=index,
                start=start,
                end=end,
                speaker=row["speaker"],
                quote=row["text"][start:end],
                delete=deletion_buffer(row["text"], start, end),
                basis=basis,
            )
        )
        for boundary in (index, index + 1):
            boundaries.setdefault(boundary, [])
            boundaries[boundary].extend(item for item in basis if item not in boundaries[boundary])
    catalog.extend(
        dict(target_id=f"B{index}", kind="boundary", before_utterance_index=index, basis=basis)
        for index, basis in sorted(boundaries.items())
    )
    if not catalog and require_targets:
        raise ValueError("targeted_revision_requires_targets")
    return catalog


def model_targets(catalog):
    return [
        dict(target_id=row["target_id"], utterance_index=row["utterance_index"], text=row["quote"])
        if row["kind"] == "text"
        else dict(target_id=row["target_id"], before_utterance_index=row["before_utterance_index"])
        for row in catalog
    ]
