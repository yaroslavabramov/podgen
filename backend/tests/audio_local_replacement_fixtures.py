"""Mechanical native-shaped records and transport doubles; never acoustic qualification."""

from uuid import uuid4

from podgen.domain.canonical import digest
from podgen.runtime.evaluation.audio.assembly.contracts import prefix as assembly_prefix
from podgen.runtime.evaluation.audio.local_replacement.approval import approve
from podgen.runtime.evaluation.audio.local_replacement.contracts import ProposalConfig
from podgen.runtime.evaluation.audio.local_replacement.prepare import prepare
from tests.audio_cases import MemoryJournal, profile, response, signal


def fixture(monkeypatch):
    journal = MemoryJournal()
    assembly_id, audit_id = uuid4(), uuid4()
    key = assembly_prefix(assembly_id)
    voice = dict(
        profile_id="fixture-gemini",
        synthesis=profile("gemini").model_dump(mode="json"),
        credential_env="TEST_SYNTHESIS_KEY",
        rates=[],
        reservation_usd="0.50",
    )
    text = "Первый текст. Второй текст. Третий текст."
    source = dict(
        source_script_ref=journal.ref("script").model_dump(mode="json"),
        source_script_hash=digest(text),
        source=dict(
            direction=dict(style=""),
            output_language="ru",
            speaker_voices={},
            utterances=[dict(utterance_id=str(uuid4()), speaker="HOST", text=text)],
        ),
    )
    source_ref = journal.put("source", source).model_dump(mode="json")
    audio = signal(rate=48000, seconds=9)
    blob = journal.blob(audio, "audio/wav")
    coverage = dict(full_script=True)
    journal.put(
        key + ":manifest",
        dict(
            source_ref=source_ref,
            coverage=coverage,
            configuration_hash="fixture",
            profile=voice,
            configuration={},
        ),
    )
    result = dict(
        contract="audio-evaluation-assembly-result/1",
        assembly_id=str(assembly_id),
        comparison_run_id=str(journal.run_id),
        manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
        source_ref=source_ref,
        source_script_ref=source["source_script_ref"],
        source_script_hash=source["source_script_hash"],
        configuration_hash="fixture",
        coverage=coverage,
        master_pcm_blob=blob,
        playback_audio_blob=blob,
        normalization=dict(applied_gain_db=0, target_true_peak_dbtp=-1),
        frame_map=[],
    )
    ref = journal.put(key + ":result", result)
    root = "fidelity:" + str(audit_id)
    journal.put(root + ":manifest", dict(audio_blob=blob, subject_ref=ref.model_dump(mode="json")))
    processing = root + ":processing:1"
    journal.put(processing, {})
    journal.put(processing + ":result", dict(fixture=True))
    segments = [
        dict(text=word, start=start, end=start + 0.4)
        for word, start in zip(text.replace(".", "").split(), [0.2, 0.7, 3.2, 3.7, 6.2, 6.7], strict=True)
    ]
    journal.put(
        processing + ":recognition",
        dict(
            submitted_audio_sha256=blob["sha256"],
            output=dict(text=text, duration=9, segments=segments, request_id="fixture"),
        ),
    )
    config = ProposalConfig(
        proposal_id=uuid4(),
        comparison_run_id=journal.run_id,
        parent_subject=dict(kind="assembly", ref=ref.model_dump(mode="json")),
        approximate_seconds=3.5,
        recognition_audit_id=audit_id,
        reason="Human heard a defect.",
    )
    monkeypatch.setenv(voice["credential_env"], "fake-local-synthesis-key")
    return journal, config, audio


def allow(journal, config, **changes):
    proposal = prepare(journal, config)
    approval = dict(
        proposal_hash=proposal["proposal_hash"],
        reason="Human approves this exact preview.",
        reservation_per_request_usd="0.50",
        max_cost_usd="1",
        max_requests=2,
    )
    approval.update(changes)
    return approve(journal, config.proposal_id, approval)


class Synthesis:
    def __init__(self, receipt=None, lost=False, seconds=2):
        self.receipt, self.lost, self.seconds, self.calls = receipt, lost, seconds, []

    def submit(self, request, credential):
        self.calls.append((request.model_dump(mode="json"), credential))
        if self.lost:
            raise RuntimeError("Lost acknowledgement")
        return self.receipt or response("gemini", signal(seconds=self.seconds))
