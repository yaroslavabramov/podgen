"""Complete speech coverage, including oversize utterances and more than two native parts."""

import json
from uuid import uuid4, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.episodes.production.audio import partition
from podgen.episodes.production.prepare import prepare
from podgen.runtime.evaluation.audio.assembly.coverage import coalesce
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.models.contracts import WireReply
from tests.episode_production_support import setup_episode
from tests.research_material_provider import ResearchProvider


def reconstruct(script, parts):
    selected = [[] for _ in script["utterances"]]
    for part in parts:
        for selection in part.selections:
            text = script["utterances"][selection.utterance_index]["text"]
            selected[selection.utterance_index].append(
                (selection.start_char, selection.end_char, text[selection.start_char : selection.end_char])
            )
    for index, chunks in enumerate(selected):
        assert chunks[0][0] == 0
        assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:], strict=False))
        assert "".join(chunk[2] for chunk in chunks) == script["utterances"][index]["text"]


def test_partition_packs_whole_utterances_and_covers_every_character():
    script = dict(
        utterances=[
            dict(speaker="A", text="Первая реплика."),
            dict(speaker="B", text="Вторая реплика."),
            dict(speaker="A", text="Конец."),
        ]
    )
    limit = sum(len(turn["text"]) for turn in script["utterances"][:2])
    parts = partition(script, RuntimeRef(record_kind="tool_execution", record_id=uuid4()), limit)
    assert len(parts) == 2
    reconstruct(script, parts)
    assert all(
        selection.start_char == 0
        and selection.end_char == len(script["utterances"][selection.utterance_index]["text"])
        for part in parts
        for selection in part.selections
    )


def test_oversized_utterance_prefers_sentences_and_preserves_unicode_and_whitespace():
    script = dict(
        utterances=[
            dict(speaker="HOST", text="Первая фраза.  Вторая — с ёжиком!\nТретья фраза. " * 25),
            dict(speaker="HOST", text="Оченьдлинноесловобезпробелов" * 30),
            dict(speaker="HOST", text="Последняя реплика."),
        ]
    )
    parts = partition(script, RuntimeRef(record_kind="tool_execution", record_id=uuid4()), 256)
    reconstruct(script, parts)
    assert len(parts) > 3
    for part in parts:
        assert sum(selection.end_char - selection.start_char for selection in part.selections) <= 256
        for selection in part.selections:
            if selection.utterance_index == 0 and selection.end_char < len(script["utterances"][0]["text"]):
                assert script["utterances"][0]["text"][selection.end_char - 1].isspace()


@pytest.mark.parametrize("start", [2, 4, 0])
def test_coalescing_rejects_overlapping_missing_or_repeated_spans(start):
    selectors = [
        dict(utterance_id="same", utterance_index=0, char_start=0, char_end=3),
        dict(utterance_id="same", utterance_index=0, char_start=start, char_end=6),
    ]
    spoken = [
        dict(utterance_id="same", speaker="HOST", text="abc"),
        dict(utterance_id="same", speaker="HOST", text="def"),
    ]
    with pytest.raises(ContractError, match="noncontiguous"):
        coalesce(selectors, spoken)


class LongScriptProvider(ResearchProvider):
    def submit(self, body, *, client_request_id):
        raw = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] == "draft":
            value = json.loads(raw.body)
            output = value["output"][0]["content"][0]
            result = json.loads(output["text"])
            speaker = result["result"]["utterances"][0]["speaker"]
            result["result"]["utterances"].extend(
                dict(
                    speaker=speaker,
                    text=f"Following observation {index}. "
                    + "The recorded chronology is examined in its stated context. " * 2,
                )
                for index in range(5)
            )
            output["text"] = json.dumps(result)
            return WireReply(json.dumps(value).encode())
        return raw


def test_full_native_render_has_more_than_two_parts_and_exact_assembly_coverage(
    repo, scope, producer, monkeypatch
):
    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    config = config.model_copy(
        update=dict(settings=config.settings.model_copy(update=dict(audio_max_part_characters=256)))
    )
    provider = LongScriptProvider()
    runner.writer.provider_factory = lambda _: provider
    prepare(repo, scope[0], config)
    result = runner.run()
    assert result["status"] == "assembled" and len(speech.calls) > 2
    assembly_ref = RuntimeRef.model_validate(result["progress"]["assembly_ref"])
    assembled = repo.get_runtime(scope[0], assembly_ref)
    audio = AudioJournal(repo, scope[0], uuid5(config.run_id, "audio-render/1"))
    assert audio.get("manifest")["contract"] == "episode-audio-render-manifest/1"
    assert len(assembled["frame_map"]) == len(speech.calls)
    assert assembled["coverage"]["full_script"] and assembled["coverage"]["utterances"] == 6
    assert not assembled["production_ready"] and assembled["acoustic_verdict"] == "pending"
    assert assembled["pcm_assembly"]["tempo_changed"] is False
    assert runner.run() == result and len(speech.calls) == len(assembled["frame_map"])


class Recognizer:
    """Hears the accepted script; the first master also carries invented sentences."""

    def __init__(self, repo, project, runner):
        self.repo, self.project, self.runner, self.calls = repo, project, runner, []

    def submit(self, request, body, credential):
        from uuid import UUID

        from podgen.audio.http import HttpReceipt
        from podgen.runtime.evaluation.journal import EvaluationJournal
        from podgen.runtime.evaluation.reading_validation import read_descriptor

        self.calls.append(request)
        writing = UUID(self.runner.progress["writing_run_id"])
        script = read_descriptor(EvaluationJournal(self.repo, self.project), writing).outcome["output"]
        words = " ".join(u["text"] for u in script["utterances"]).split()
        if len(self.calls) == 1:
            middle = len(words) // 2
            words[middle:middle] = "Всё осложняется тем что крюка надо два".split()
        text = " ".join(words)
        segments = [dict(text=word, start=i * 0.01, end=i * 0.01 + 0.008) for i, word in enumerate(words)]
        body = dict(text=text, duration=len(words) * 0.01, segments=segments, request_id="fixture")
        return HttpReceipt(200, {"content-type": "application/json"}, json.dumps(body).encode(), 20)


def test_invented_content_is_resynthesized_once_and_verified_again(repo, scope, producer, monkeypatch):
    from podgen.episodes.production.contracts import AudioVerification

    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    price = dict(usd_per_audio_minute="0.006", source_url="https://docs.fish.audio", checked_at="2026-11-23")
    settings = config.settings.model_copy(
        update=dict(audio_max_part_characters=256, audio_verification=AudioVerification(price=price))
    )
    config = config.model_copy(update=dict(settings=settings))
    runner.writer.provider_factory = lambda _: LongScriptProvider()
    monkeypatch.setenv("FISH_API_KEY", "fixture")
    recognizer = Recognizer(repo, scope[0], runner)
    runner.recognition_transport = recognizer
    prepare(repo, scope[0], config)
    result = runner.run()
    assert result["status"] == "assembled"
    verification = result["progress"]["audio_verification"]
    assert verification["repair_round"] == 1 and verification["automatic_repair"] == 0
    assembled = repo.get_runtime(scope[0], RuntimeRef.model_validate(result["progress"]["assembly_ref"]))
    parts = len(assembled["frame_map"])
    assert len(speech.calls) == parts + 1 and len(recognizer.calls) == 2
    from podgen.runtime.evaluation.audio.attempts import prefix as take_prefix

    audio = AudioJournal(repo, scope[0], uuid5(config.run_id, "audio-render/2"))
    used = {part["source_result_ref"]["record_id"] for part in assembled["frame_map"]}
    retakes = [
        cell["cell_id"]
        for cell in audio.get("manifest")["cells"]
        if audio.get(take_prefix(cell["cell_id"], 2) + ":prepared")
    ]
    assert len(retakes) == 1
    assert str(audio.ref(take_prefix(retakes[0], 2) + ":result").record_id) in used
    assert runner.run() == result
    assert len(speech.calls) == parts + 1 and len(recognizer.calls) == 2
