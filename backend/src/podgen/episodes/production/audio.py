"""Full ordered rendering using the existing synthesis, attempt and assembly owners."""

import re
from typing import Literal
from uuid import uuid5

from pydantic import Field

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.assembly.contracts import AssemblyConfig
from podgen.runtime.evaluation.audio.assembly.service import build
from podgen.runtime.evaluation.audio.attempts import OperationLedger, execute_sample
from podgen.runtime.evaluation.audio.contracts import Fragment, TargetedComparisonConfig
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.audio.prepare import prepare
from podgen.runtime.evaluation.audio.results import ASSEMBLY_INPUT
from podgen.runtime.evaluation.reading_validation import read_descriptor


class FullRenderConfig(TargetedComparisonConfig):
    contract: Literal["episode-audio-render/1"] = "episode-audio-render/1"
    fragments: list[Fragment] = Field(min_length=1, max_length=4096)

    @property
    def manifest_contract(self):
        return "episode-audio-render-manifest/1"

    @property
    def processing_contract(self):
        return ASSEMBLY_INPUT


def partition(script, outcome_ref, limit):
    fragments, selections, size = [], [], 0

    def flush():
        nonlocal selections, size
        if selections:
            fragments.append(
                Fragment(
                    fragment_id=f"part-{len(fragments) + 1:04d}",
                    outcome_ref=outcome_ref,
                    selections=selections,
                )
            )
            selections, size = [], 0

    for index, utterance in enumerate(script["utterances"]):
        text, start = utterance["text"], 0
        if not text:
            raise ContractError("episode_audio_empty_utterance")
        if size + len(text) > limit:
            flush()
        if len(text) <= limit:
            selections.append(dict(utterance_index=index, start_char=0, end_char=len(text)))
            size += len(text)
            continue
        boundaries = [m.end() for m in re.finditer(r'[.!?…][»"\)\]]*\s+|\n+', text)]
        while start < len(text):
            if size == limit:
                flush()
            end = min(len(text), start + limit - size)
            if end < len(text):
                natural = [boundary for boundary in boundaries if start < boundary <= end]
                if natural:
                    end = natural[-1]
                else:
                    boundary = text.rfind(" ", start, end)
                    if boundary > start:
                        end = boundary + 1
            selections.append(dict(utterance_index=index, start_char=start, end_char=end))
            size += end - start
            start = end
            if start < len(text):
                flush()
    flush()
    return fragments


def configuration(config, outcome, script):
    profile = config.settings.audio_profile
    provider = profile.synthesis.provider
    parts = partition(script, outcome, config.settings.audio_max_part_characters)
    return FullRenderConfig(
        run_id=uuid5(config.run_id, "audio-render/1"),
        providers=[provider],
        profiles=[profile],
        fragments=parts,
        expected_voices_per_provider={provider: 1},
        max_cost_usd=config.settings.audio_max_cost_usd,
        max_attempts=len(parts),
        request_timeout_seconds=900,
        blind_seed=0,
    )


def render(parent, config, progress, transport):
    writing_run = progress["writing_run_id"]
    from uuid import UUID

    from podgen.runtime.evaluation.journal import EvaluationJournal

    validated = read_descriptor(EvaluationJournal(parent.repo, parent.project), UUID(writing_run))
    checked = validated.outcome if validated else None
    if checked is None or checked["status"] != "keep":
        raise ContractError("episode_audio_requires_native_keep_outcome")
    outcome = RuntimeRef.model_validate(progress["outcome_ref"])
    render_config = configuration(config, outcome, checked["output"])
    audio = AudioJournal(parent.repo, parent.project, render_config.run_id)
    with audio.lock():
        manifest = prepare(audio, render_config, outcomes=[validated])
        ledger = OperationLedger(audio, manifest)
        refs = []
        samples = []
        for cell in manifest["cells"]:
            result = execute_sample(audio, manifest, cell, render_config, transport, operation_ledger=ledger)
            if result.status != "completed":
                raise ContractError("episode_audio_" + result.status)
            refs.append(RuntimeRef.model_validate(result.result_ref))
            samples.append(result)
    assembly = AssemblyConfig(
        assembly_id=uuid5(config.run_id, "audio-assembly/1"),
        comparison_run_id=audio.run_id,
        part_result_refs=refs,
    )
    ref = build(audio, assembly, samples=samples, outcomes=[validated])
    return audio, assembly, ref
