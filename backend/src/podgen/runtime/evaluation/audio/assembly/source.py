from podgen.audio.synthesis.contracts import SparseDirection
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from ..contracts import Fragment, UtteranceSelection
from ..source import import_fragment
from ..subjects import comparison_results, synthesis_part


def prepare_sources(journal, config, *, samples=None, outcomes=()):
    parts = (
        comparison_results(
            journal, config.part_result_refs, **(dict(samples=samples) if samples is not None else {})
        )
        if config.contract == "audio-evaluation-assembly/1"
        else [synthesis_part(journal, b) for b in config.bindings]
    )
    if config.contract == "audio-evaluation-assembly/2":
        from .legacy_repair import validate_replacements

        validate_replacements(journal, config)
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
        {RuntimeRef.model_validate(value.binding["records"][0]["ref"]): value for value in outcomes},
    )
    selectors = [s for p in parts for s in p["source"]["selectors"]]
    utterances = [u for p in parts for u in p["source"]["source"]["utterances"]]
    from .coverage import coalesce

    selectors, utterances = coalesce(selectors, utterances)
    if selectors != full["selectors"] or utterances != full["source"]["utterances"]:
        raise ContractError("assembly_requires_full_ordered_utterance_coverage")
    return full, parts
