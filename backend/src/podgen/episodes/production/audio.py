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
    verification = config.settings.audio_verification
    # A verified render may retake every part once per repair round.
    return FullRenderConfig(
        run_id=uuid5(config.run_id, "audio-render/2" if verification else "audio-render/1"),
        providers=[provider],
        profiles=[profile],
        fragments=parts,
        expected_voices_per_provider={provider: 1},
        max_cost_usd=config.settings.audio_max_cost_usd,
        max_attempts=len(parts) * (1 + verification.max_repair_rounds) if verification else len(parts),
        request_timeout_seconds=900,
        blind_seed=0,
    )


def render(parent, config, progress, transport, recognition_transport=None):
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
        refs, samples = [], []
        for cell in manifest["cells"]:
            result = _sample(audio, manifest, cell, render_config, transport, ledger)
            refs.append(RuntimeRef.model_validate(result.result_ref))
            samples.append(result)
    verification = config.settings.audio_verification
    assembly = AssemblyConfig(
        assembly_id=uuid5(config.run_id, "audio-assembly/1"),
        comparison_run_id=audio.run_id,
        part_result_refs=refs,
    )
    ref = build(audio, assembly, samples=samples, outcomes=[validated])
    if verification is None:
        return audio, assembly, ref, None
    # Review and finding-based repair need a recognition of exactly the master the user hears,
    # so each assembled master is recognized once; a resumed run reuses the saved result.
    audits, derived = [], None
    for round_index in range(verification.max_repair_rounds + 1):
        if round_index == 0:
            audit, report = _verify(
                audio,
                config,
                verification,
                dict(kind="assembly", ref=ref),
                "audio-verification/1:0",
                recognition_transport,
            )
            audits.append(str(audit))
        else:
            audit, report = derived, _report(audio, derived)
        repairs = [c for c in report["candidates"] if c["review_route"] == "automatic_repair"]
        if not repairs:
            routes = [candidate["review_route"] for candidate in report["candidates"]]
            summary = dict(
                contract="episode-audio-verification/3",
                audits=audits,
                final_audit_id=str(audit),
                master=dict(kind="assembly", ref=ref),
                repair_rounds=round_index,
                **{
                    r: routes.count(r)
                    for r in ("automatic_repair", "user_confirmation", "recognition_variant")
                },
            )
            return audio, assembly, ref, summary
        if round_index == verification.max_repair_rounds:
            break
        frame_map = audio.repo.get_runtime(audio.project, RuntimeRef.model_validate(ref))["frame_map"]
        affected = sorted(
            {
                position
                for candidate in repairs
                for position, part in enumerate(frame_map)
                # The review clip is padded on both sides; the heard words are not.
                if part["master_start_frame"]
                < candidate["audio_range"]["end_frame"] - candidate["audio_range"]["margin_frames"]
                and candidate["audio_range"]["start_frame"] + candidate["audio_range"]["margin_frames"]
                < part["master_end_frame"]
            }
        )
        reason = f"fidelity_automatic_repair:{audit}"
        with audio.lock():
            for position in affected:
                cell = manifest["cells"][position]
                retaken = any(row["prepared"].get("reason") == reason for row in ledger.rows[cell["cell_id"]])
                _sample(audio, manifest, cell, render_config, transport, ledger, None if retaken else reason)
                # The sample descriptor names the baseline take; the new master uses the repair take.
                take = next(
                    row["take"]
                    for row in ledger.rows[cell["cell_id"]]
                    if row["prepared"].get("reason") == reason
                )
                result = ledger.result(cell, "completed", take=take)
                if result.result_ref is None:
                    raise ContractError("episode_audio_repair_take_unusable")
                refs[position], samples[position] = RuntimeRef.model_validate(result.result_ref), result
        # Only re-synthesized parts are recognized; the new master's recognition is composed.
        parts = {}
        for position in affected:
            parts[position], _ = _verify(
                audio,
                config,
                verification,
                dict(kind="comparison_result", ref=refs[position]),
                f"audio-verification/1:{round_index + 1}:part:{position}",
                recognition_transport,
            )
            audits.append(str(parts[position]))
        assembly = AssemblyConfig(
            assembly_id=uuid5(config.run_id, f"audio-assembly/1:repair:{round_index + 1}"),
            comparison_run_id=audio.run_id,
            part_result_refs=refs,
        )
        ref = build(audio, assembly, samples=samples, outcomes=[validated])
        derived = _derive(audio, config, verification, ref, round_index + 1, audit, frame_map, parts)
        audits.append(str(derived))
    raise ContractError("episode_audio_repair_exhausted")


def _sample(audio, manifest, cell, config, transport, ledger, retake_reason=None):
    result = execute_sample(
        audio, manifest, cell, config, transport, retake_reason=retake_reason, operation_ledger=ledger
    )
    if result.status != "completed":
        raise ContractError("episode_audio_" + result.status)
    return result


def audit_config(audio, config, settings, subject, name):
    """The exact recognition audit of one master; the same name always yields the same audit."""
    from podgen.runtime.evaluation.audio.fidelity.contracts import FidelityConfigV2
    from podgen.runtime.evaluation.audio.subject_contracts import AudioSubject

    return FidelityConfigV2(
        audit_id=uuid5(config.run_id, name),
        comparison_run_id=audio.run_id,
        recognition=settings.recognition,
        credential_env=settings.credential_env,
        price=settings.price,
        max_cost_usd=settings.max_cost_usd,
        reservation_usd=settings.reservation_usd,
        subject=AudioSubject.model_validate(subject),
    )


def _report(audio, audit_id):
    from podgen.runtime.evaluation.audio.fidelity.processing import revisions

    return [row for row in revisions(audio, audit_id) if row["result"]][-1]["result"]["report"]


def _recognition(audio, audit_id):
    from podgen.audio.recognition.contracts import Recognition
    from podgen.runtime.evaluation.audio.fidelity.processing import revisions

    row = [row for row in revisions(audio, audit_id) if row["result"]][-1]
    return Recognition.model_validate(audio.get(row["key"] + ":recognition")["output"])


def _derive(audio, config, settings, ref, round_index, previous, previous_map, parts):
    """Recognition of a reassembled master from the previous master's words and the new parts' words."""
    from podgen.audio.recognition.contracts import Recognition
    from podgen.runtime.evaluation.audio.fidelity.processing import derive

    assembled = audio.repo.get_runtime(audio.project, RuntimeRef.model_validate(ref))
    rate = assembled["normalization"]["sample_rate_hz"]
    old = _recognition(audio, previous)
    segments = []
    for position, part in enumerate(assembled["frame_map"]):
        start = part["master_start_frame"] / rate
        if position in parts:
            words = [(s.text, s.start, s.end) for s in _recognition(audio, parts[position]).segments]
        else:
            before = previous_map[position]
            low, high = before["master_start_frame"] / rate, before["master_end_frame"] / rate
            words = [
                (s.text, s.start - low, s.end - low)
                for s in old.segments
                if low <= (s.start + s.end) / 2 < high
            ]
        segments.extend(
            dict(text=text, start=max(0, a + start), end=max(0, b + start)) for text, a, b in words
        )
    composed = Recognition(
        text=" ".join(segment["text"] for segment in segments),
        duration=assembled["frame_map"][-1]["master_end_frame"] / rate,
        segments=segments,
        request_id="derived",
    )
    name = f"audio-verification/1:{round_index}"
    audit = audit_config(audio, config, settings, dict(kind="assembly", ref=ref), name)
    sources = dict(
        contract="episode-derived-recognition-sources/1",
        previous_audit_id=str(previous),
        part_audit_ids={str(position): str(audit_id) for position, audit_id in sorted(parts.items())},
    )
    if derive(audio, audit, composed, sources) != "candidates_generated":
        raise ContractError("episode_audio_derived_recognition_failed")
    return audit.audit_id


def _verify(audio, config, settings, subject, name, transport):
    """One recognition of this master; resume reuses its saved result."""
    from podgen.audio.http import RawAudioTransport
    from podgen.runtime.evaluation.audio.fidelity.prepare import prepare as prepare_audit
    from podgen.runtime.evaluation.audio.fidelity.processing import revisions
    from podgen.runtime.evaluation.audio.fidelity.service import run as recognize

    audit = audit_config(audio, config, settings, subject, name)
    with audio.lock():
        prepare_audit(audio, audit)
    status = recognize(
        audio,
        audit,
        transport or RawAudioTransport(timeout_seconds=settings.recognition.request_timeout_seconds),
    )
    if status != "candidates_generated":
        raise ContractError("episode_audio_verification_" + status)
    return audit.audit_id, revisions(audio, audit.audit_id)[-1]["result"]["report"]
