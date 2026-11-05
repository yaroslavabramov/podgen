"""Unaccepted original editorial material; deliberately not a DossierSnapshot/StoryPlan/ScriptRevision."""

import re
from typing import Literal

from .contracts.common import BlobRef, Contract, NonNegativeInt, RuntimeRef, Text


class StagedUtterance(Contract):
    ordinal: NonNegativeInt
    speaker_label: Text
    text: Text
    char_start: NonNegativeInt
    char_end: NonNegativeInt


class StagedEditorialFile(Contract):
    contract: Literal["editorial-staging-v1"]
    source_path: Text
    material_kind: Literal[
        "author_input", "source_material", "script_draft", "script_final", "evaluator_material", "provenance"
    ]
    technical_fixture: Literal[True]
    semantic_acceptance: Literal[False]
    original_blob: BlobRef
    full_text: str | None
    staged_spoken_view: list[StagedUtterance]
    prior_staged_script_ref: RuntimeRef | None
    original_author_kind: Literal["model", "external_source", "mixed_package_metadata"]
    exact_model_id: None
    model_invocation_archive: None
    independent_approvals: list[str]
    mapping_status: Literal["unmapped"]
    remaining_mapping: list[str]


def staged_spoken_view(text):
    labels = list(re.finditer(r"(?m)^([А-ЯЁ]+):[ \t]+", text))
    if not labels or text[: labels[0].start()].strip():
        raise ValueError("staging_script_has_unmapped_content")
    result = []
    for index, label in enumerate(labels):
        start = label.end()
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        while end > start and text[end - 1] in "\r\n":
            end -= 1
        spoken = text[start:end]
        if not spoken.strip():
            raise ValueError("empty_staged_utterance")
        result.append(
            StagedUtterance(
                ordinal=index, speaker_label=label[1], text=spoken, char_start=start, char_end=end
            )
        )
    return result
