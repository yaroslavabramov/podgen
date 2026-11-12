from podgen.audio.synthesis.contracts import SparseDirection
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from ..contracts import Fragment, UtteranceSelection
from ..source import import_fragment
from ..subjects import comparison_result


def prepare_sources(journal, config):
    parts = [comparison_result(journal, ref) for ref in config.part_result_refs]
    first = parts[0]

    def identity(p):
        return dict(
            profile=p["profile"],
            script_ref=p["source"]["source_script_ref"],
            script_hash=p["source"]["source_script_hash"],
            outcome_ref=p["source"]["outcome_ref"],
            direction=p["source"]["source"]["direction"],
        )

    if any(digest(identity(p)) != digest(identity(first)) for p in parts[1:]):
        raise ContractError("assembly_parts_profile_style_or_script_mismatch")
    outcome_ref = RuntimeRef.model_validate(first["source"]["outcome_ref"])
    outcome = journal.base.read(outcome_ref)
    full = import_fragment(
        journal,
        Fragment(
            fragment_id="assembly-full-source",
            outcome_ref=outcome_ref,
            selections=[
                UtteranceSelection(utterance_index=i) for i in range(len(outcome["output"]["utterances"]))
            ],
            direction=SparseDirection.model_validate(first["source"]["source"]["direction"]),
        ),
        {},
    )
    selectors = [s for p in parts for s in p["source"]["selectors"]]
    utterances = [u for p in parts for u in p["source"]["source"]["utterances"]]
    if selectors != full["selectors"] or utterances != full["source"]["utterances"]:
        raise ContractError("assembly_requires_full_ordered_utterance_coverage")
    return full, parts
