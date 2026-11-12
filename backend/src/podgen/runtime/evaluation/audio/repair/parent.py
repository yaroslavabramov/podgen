from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from ..subject_contracts import SynthesisPart
from ..subjects import audio_subject


def parts(journal, parent):
    subject = audio_subject(journal, parent)
    if parent.kind == "comparison_result":
        return subject, [SynthesisPart(kind="comparison_result", ref=parent.ref)]
    return subject, [
        SynthesisPart(
            kind=p.get("source_kind", "comparison_result"),
            ref=RuntimeRef.model_validate(p["source_result_ref"]),
        )
        for p in subject["result"]["frame_map"]
    ]


def index(journal, parent, target):
    subject, bindings = parts(journal, parent)
    if target not in bindings:
        raise ContractError("repair_part_outside_parent_subject")
    return subject, bindings, bindings.index(target)
