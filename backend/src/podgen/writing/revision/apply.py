"""Apply nonoverlapping edits to the immutable source coordinates, never to model-copied text."""

from collections import defaultdict
from copy import deepcopy

from podgen.domain.canonical import digest

from ..roles import WriteOutput
from .buffers import normalize_edits, replacement_separator_check
from .contracts import SequenceRevision, StructuralRevision, TargetedRevision
from .sequences import replacements as sequence_replacements


def _validate_script(script, speakers):
    result = WriteOutput.model_validate(script).model_dump(mode="json")
    if {row["speaker"] for row in result["utterances"]} != set(speakers):
        raise ValueError("speaker_labels_mismatch")
    return result


def apply_targeted(script, revision, catalog, speakers, *, expected_hash, sequences=(), sequence_catalog=()):
    if digest(script) != expected_hash:
        raise ValueError("revision_source_version_mismatch")
    offered = {row["target_id"]: row for row in catalog}
    spans = sequence_replacements(script, sequences, sequence_catalog)
    edits, permissions, insertions, trace = defaultdict(list), defaultdict(list), {}, []
    for index, edit in enumerate(revision.edits):
        target = offered.get(edit.target_id)
        if target is None:
            raise ValueError("revision_target_not_offered")
        if edit.operation == "insert":
            if target["kind"] != "boundary":
                raise ValueError("revision_target_kind_mismatch")
            boundary = target["before_utterance_index"]
            if not 0 <= boundary <= len(script["utterances"]):
                raise ValueError("revision_boundary_invalid")
            if boundary in insertions:
                raise ValueError("revision_insertions_overlap")
            if any(start < boundary < end for start, (end, _) in spans.items()):
                raise ValueError("revision_edits_overlap_sequence")
            insertions[boundary] = [row.model_dump(mode="json") for row in edit.utterances]
            effect = dict(before_utterance_index=boundary)
        else:
            if target["kind"] != "text":
                raise ValueError("revision_target_kind_mismatch")
            original = script["utterances"][target["utterance_index"]]
            if any(start <= target["utterance_index"] < end for start, (end, _) in spans.items()):
                raise ValueError("revision_edits_overlap_sequence")
            if (
                original["speaker"] != target["speaker"]
                or original["text"][target["start"] : target["end"]] != target["quote"]
            ):
                raise ValueError("revision_target_source_mismatch")
            replacement = edit.text if edit.operation == "replace" else ""
            if replacement == target["quote"]:
                raise ValueError("revision_edit_is_noop")
            permissions[target["utterance_index"]].append((target["start"], target["end"]))
            if edit.operation == "delete":
                buffered = target["delete"]
                start, end = buffered["start"], buffered["end"]
                if original["text"][start:end] != buffered["text"]:
                    raise ValueError("revision_target_source_mismatch")
            else:
                start, end = target["start"], target["end"]
                replacement_separator_check(original["text"], start, end, replacement)
            edits[target["utterance_index"]].append((start, end, replacement))
            effect = dict(
                utterance_index=target["utterance_index"],
                start=start,
                end=end,
                before=original["text"][start:end],
                after=replacement,
            )
        trace.append(
            dict(operation_index=index, operation=edit.operation, target_id=edit.target_id, **effect)
        )
    for spans_allowed in permissions.values():
        cursor = -1
        for start, end in sorted(spans_allowed):
            if start < cursor:
                raise ValueError("revision_text_edits_overlap")
            cursor = end
    output = []
    index = 0
    while index <= len(script["utterances"]):
        output.extend(insertions.get(index, []))
        if index == len(script["utterances"]):
            break
        if index in spans:
            index, selected = spans[index]
            output.extend(selected)
            continue
        row = script["utterances"][index]
        if index not in edits:
            output.append(deepcopy(row))
            index += 1
            continue
        cursor, pieces = 0, []
        for start, end, text in normalize_edits(row["text"], edits[index]):
            pieces.extend((row["text"][cursor:start], text))
            cursor = end
        pieces.append(row["text"][cursor:])
        text = "".join(pieces)
        if text:
            output.append(dict(speaker=row["speaker"], text=text))
        index += 1
    result = _validate_script(dict(utterances=output), speakers)
    if result == script:
        raise ValueError("revision_is_noop")
    return result, dict(
        base_script_hash=digest(script),
        result_script_hash=digest(result),
        edits=trace,
        unchanged_outside_targets=True,
        sequences=[dict(start=start, end=end) for start, (end, _) in spans.items()],
    )


def materialize(
    mode, wire_output, *, script, catalog, speakers, composition, expected_hash, sequence_catalog=()
):
    if digest(script) != expected_hash:
        raise ValueError("revision_source_version_mismatch")
    if mode == "targeted":
        revision = TargetedRevision.model_validate(wire_output)
        output, application = apply_targeted(script, revision, catalog, speakers, expected_hash=expected_hash)
        selected = revision.composition_update
        selected = selected.model_dump(mode="json") if selected is not None else deepcopy(composition)
    elif mode == "sequence":
        revision = SequenceRevision.model_validate(wire_output)
        output, application = apply_targeted(
            script,
            revision,
            catalog,
            speakers,
            expected_hash=expected_hash,
            sequences=revision.sequences,
            sequence_catalog=sequence_catalog,
        )
        selected = revision.composition.model_dump(mode="json")
    elif mode == "structural":
        revision = StructuralRevision.model_validate(wire_output)
        output = _validate_script(revision.script.model_dump(mode="json"), speakers)
        if output == script:
            raise ValueError("revision_is_noop")
        selected = revision.composition.model_dump(mode="json")
        application = dict(base_script_hash=digest(script), result_script_hash=digest(output))
    else:
        raise ValueError("unknown_revision_mode")
    return output, selected, application
