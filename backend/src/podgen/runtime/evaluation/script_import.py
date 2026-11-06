"""An authorized saved complete script is a first-class starting version, never an author hint."""

import hashlib
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.roles import WriteOutput

from .journal import EvaluationJournal
from .scripts import script_view
from .validation import load_unique_json


def import_script(repo, project, raw):
    data = load_unique_json(raw)
    script = WriteOutput.model_validate(data).model_dump(mode="json")
    journal = EvaluationJournal(repo, project)
    ref = RuntimeRef(
        record_kind="tool_execution",
        record_id=uuid5(project, "imported-script:" + hashlib.sha256(raw).hexdigest()),
    )
    original = journal.blob(raw)
    journal.record(
        dict(
            contract="imported-writing-script/1",
            output=script,
            script_hash=digest(script),
            original_blob=original.model_dump(mode="json"),
            spoken_view=script_view(ref.record_id, script),
        ),
        record_id=ref.record_id,
    )
    return ref


def load_script(generation, project, ref, speaker_labels):
    record = generation.get_runtime(project, ref)
    if record.get("contract") not in (
        "imported-writing-script/1",
        "writing-evaluation-output-v1",
        "writing-finalization-v1",
        "independent-reading-output/1",
        "independent-reading-outcome/1",
    ):
        raise ContractError("unsupported_initial_script_record")
    script = WriteOutput.model_validate(record["output"]).model_dump(mode="json")
    if {row["speaker"] for row in script["utterances"]} != set(speaker_labels):
        raise ContractError("initial_script_speaker_mismatch")
    return script
