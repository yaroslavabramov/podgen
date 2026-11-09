"""A server-bound revision protocol; public outputs remain complete scripts."""

from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError
from podgen.writing import prompt_cache
from podgen.writing.revision.apply import materialize
from podgen.writing.revision.contracts import VERSION, SequenceRevision, StructuralRevision, TargetedRevision
from podgen.writing.revision.scopes import select_mode
from podgen.writing.revision.sequences import sequence_catalog
from podgen.writing.revision.targets import model_targets, target_catalog

from .reading_workflow import independent


def supports(command):
    return (
        independent(command)
        and command.input_contract == "editorial-role-packets/1"
        and getattr(command, "writing_strategy", "whole_episode") == "whole_episode"
    )


def enabled(manifest):
    value = manifest.get("script_revision_contract")
    if value is None:
        return False
    if value != VERSION:
        raise ContractError("unknown_script_revision_contract")
    return True


def schemas(*, prompt_cache_layout=None):
    prompt_cache.validate(prompt_cache_layout)
    result = {
        "targeted": TargetedRevision.model_json_schema(),
        "sequence": SequenceRevision.model_json_schema(),
        "structural": StructuralRevision.model_json_schema(),
    }
    if prompt_cache_layout is not None:
        for mode in ("targeted", "sequence"):
            definitions = result[mode]["$defs"]
            for model, pattern in (
                ("ReplaceText", r"^T[1-9][0-9]*$"),
                ("DeleteText", r"^T[1-9][0-9]*$"),
                ("InsertUtterances", r"^B[0-9]+$"),
            ):
                definitions[model]["properties"]["target_id"]["pattern"] = pattern
            if mode == "sequence":
                definitions["SequenceEdit"]["properties"]["range_id"]["pattern"] = r"^R[1-9][0-9]*$"
    return result


def validate_manifest(manifest):
    if enabled(manifest) and manifest.get("revision_response_schemas") != schemas(
        prompt_cache_layout=prompt_cache.from_manifest(manifest)
    ):
        raise Conflict("revision_output_contract_changed")


def operation(script, readings, version):
    try:
        mode = select_mode(readings)
        return dict(
            mode=mode,
            script_version=version,
            targets=target_catalog(script, readings, require_targets=mode == "targeted")
            if mode != "structural"
            else [],
            sequences=sequence_catalog(script, readings) if mode == "sequence" else [],
        )
    except (ValueError, KeyError) as error:
        raise ContractError(str(error)) from None


def bound_schema(manifest, selection):
    result = deepcopy(manifest["revision_response_schemas"][selection["mode"]])
    if prompt_cache.from_manifest(manifest) is not None:
        return result
    if selection["mode"] != "structural":
        if not selection["targets"]:
            result["properties"]["edits"]["maxItems"] = 0
        for model, kind in (
            ("ReplaceText", "text"),
            ("DeleteText", "text"),
            ("InsertUtterances", "boundary"),
        ):
            ids = [row["target_id"] for row in selection["targets"] if row["kind"] == kind]
            if ids:
                result["$defs"][model]["properties"]["target_id"]["enum"] = ids
        if selection["mode"] == "sequence":
            result["$defs"]["SequenceEdit"]["properties"]["range_id"]["enum"] = [
                row["range_id"] for row in selection["sequences"]
            ]
    return result


def model_operation(selection):
    return dict(
        mode=selection["mode"],
        script_version=selection["script_version"],
        targets=model_targets(selection["targets"]),
        sequences=[
            dict(
                range_id=row["range_id"],
                first_utterance_index=row["start"],
                last_utterance_index=row["end"] - 1,
            )
            for row in selection["sequences"]
        ],
    )


def output_model(selection):
    return {"targeted": TargetedRevision, "sequence": SequenceRevision, "structural": StructuralRevision}[
        selection["mode"]
    ]


def apply(selection, wire, *, script, composition, speakers, expected_hash, unresolved_questions=None):
    output, state, proof = materialize(
        selection["mode"],
        wire,
        script=script,
        catalog=selection["targets"],
        speakers=speakers,
        composition=composition,
        expected_hash=expected_hash,
        sequence_catalog=selection["sequences"],
    )
    if state is not None:
        questions = (
            unresolved_questions
            if unresolved_questions is not None
            else (composition or {}).get("unresolved_questions", [])
        )
        state["unresolved_questions"] = deepcopy(list(questions))
    record = dict(
        contract=VERSION, operation=deepcopy(selection), wire_output=deepcopy(wire), application=proof
    )
    return output, state, record


def verify_record(record, *, script, composition, readings, speakers, version, unresolved_questions=None):
    revision = record["revision"]
    selection = operation(script, readings, version)
    output, state, replay = apply(
        selection,
        revision["wire_output"],
        script=script,
        composition=composition,
        speakers=speakers,
        expected_hash=digest(script),
        unresolved_questions=unresolved_questions,
    )
    if revision != replay or record["output"] != output or record["composition"] != state:
        raise Conflict("revision_application_mismatch")
