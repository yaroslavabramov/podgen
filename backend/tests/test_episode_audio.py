"""Complete speech coverage, including oversize utterances and more than two native parts."""

import json
from uuid import UUID, uuid4, uuid5

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


def texts(value):
    """Every text field of a provider request body, in order."""
    if isinstance(value, dict):
        return [
            t for k, v in value.items() for t in ([v] if k == "text" and isinstance(v, str) else texts(v))
        ]
    return [t for v in value for t in texts(v)] if isinstance(value, list) else []


class Recognizer:
    """The first master carries invented sentences; a re-synthesized part says exactly its text."""

    def __init__(self, repo, project, runner, run_id, misheard=False):
        self.repo, self.project, self.runner, self.calls = repo, project, runner, []
        self.run_id, self.misheard, self.spliced = run_id, misheard, None

    def submit(self, request, body, credential):
        from podgen.audio.http import HttpReceipt
        from podgen.runtime.evaluation.audio.attempts import prefix as take_prefix

        self.calls.append(request)
        if self.spliced is not None:
            # Localization of a local replacement hears exactly its synthesis text.
            parts = [" ".join(u["text"] for u in self.spliced()["synthesis_input"]["utterances"]).split()]
        else:
            audio = AudioJournal(self.repo, self.project, uuid5(self.run_id, "audio-render/2"))
            cells = audio.get("manifest")["cells"]
            if len(self.calls) == 1:
                # Each 3-second part says its own words; the middle part also invents a sentence.
                parts = [" ".join(texts(cell["request"]["body"])).split() for cell in cells]
                parts[len(parts) // 2][1:1] = "Всё осложняется тем что крюка надо два".split()
            else:
                cell = next(c for c in cells if audio.get(take_prefix(c["cell_id"], 2) + ":prepared"))
                parts = [" ".join(texts(cell["request"]["body"])).split()]
                if self.misheard:
                    parts[0][parts[0].index("chronology")] = "chronicle"
        words, segments = [], []
        for index, said in enumerate(parts):
            step = 3 / (len(said) + 1)
            segments += [
                dict(text=w, start=index * 3 + (i + 0.2) * step, end=index * 3 + (i + 0.7) * step)
                for i, w in enumerate(said)
            ]
            words += said
        value = dict(text=" ".join(words), duration=len(parts) * 3, segments=segments, request_id="fixture")
        return HttpReceipt(200, {"content-type": "application/json"}, json.dumps(value).encode(), 20)


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
    recognizer = Recognizer(repo, scope[0], runner, config.run_id)
    runner.recognition_transport = recognizer
    prepare(repo, scope[0], config)
    result = runner.run()
    assert result["status"] == "assembled"
    verification = result["progress"]["audio_verification"]
    assert verification["repair_rounds"] == 1 and verification["automatic_repair"] == 0
    # The first master, the re-synthesized part and the composed recognition of the new master.
    assert len(verification["audits"]) == 3 and verification["final_audit_id"] == verification["audits"][-1]
    assert verification["master"]["ref"] == result["progress"]["assembly_ref"]
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


def test_a_confirmed_finding_is_repaired_and_the_listened_master_accepted(
    repo, scope, producer, monkeypatch, tmp_path
):
    from podgen.episodes.production import review
    from podgen.episodes.production.contracts import AudioVerification
    from podgen.episodes.production.journal import ProductionJournal
    from podgen.runtime.evaluation.audio.local_replacement.contracts import prefix as proposal_prefix

    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    price = dict(usd_per_audio_minute="0.006", source_url="https://docs.fish.audio", checked_at="2026-11-23")
    settings = config.settings.model_copy(
        update=dict(audio_max_part_characters=256, audio_verification=AudioVerification(price=price))
    )
    config = config.model_copy(update=dict(settings=settings))
    runner.writer.provider_factory = lambda _: LongScriptProvider()
    monkeypatch.setenv("FISH_API_KEY", "fixture")
    recognizer = Recognizer(repo, scope[0], runner, config.run_id, misheard=True)
    runner.recognition_transport = recognizer
    prepare(repo, scope[0], config)
    assert runner.run()["status"] == "assembled"
    journal = ProductionJournal(repo, scope[0], config.run_id)
    page = review.export_review(repo, scope[0], journal.read(), tmp_path / "review")
    assert (tmp_path / "review/index.html").exists()
    status = json.loads((tmp_path / "review/recognition/status.json").read_text())
    report = json.loads(
        (tmp_path / "review/recognition" / status["current"]["current_result"]["path"]).read_text()
    )
    found = [c for c in report["report"]["candidates"] if c["review_route"] == "user_confirmation"]
    assert [" ".join(t["text"] for t in c["expected_tokens"]) for c in found] == ["chronology"]
    candidate = UUID(found[0]["candidate_id"])
    audio = AudioJournal(repo, scope[0], uuid5(config.run_id, "audio-render/2"))
    proposal_id = uuid5(config.run_id, f"episode-repair:{page['audit_id']}:{candidate}")
    recognizer.spliced = lambda: audio.get(proposal_prefix(proposal_id) + ":proposal")["payload"]
    synthesized = len(speech.calls)
    row = review.repair(repo, scope[0], journal, [candidate], speech, recognizer, "Heard and confirmed.")
    assert row["progress"]["audio_master"]["kind"] == "local_master" and len(speech.calls) == synthesized + 1
    repaired = row["progress"]["audio_repairs"]["proposals"][0]
    assert "chronology" in repaired["text"] and repaired["target_check"]["heard_as_written"]
    with pytest.raises(ContractError, match="episode_repair_requires_findings_of_the_current_master"):
        review.repair(repo, scope[0], journal, [candidate], speech, recognizer, "Again.")
    accepted = review.accept(repo, scope[0], journal, "Listened to the whole master.")
    assert accepted["status"] == "accepted"
    assert accepted["progress"]["audio_acceptance"]["master"] == row["progress"]["audio_master"]
    assert review.accept(repo, scope[0], journal, "Listened to the whole master.") == accepted
    package = review.export_package(repo, scope[0], journal.read(), tmp_path / "package")
    assert {p.name for p in (tmp_path / "package").iterdir()} == {
        "master.wav",
        "script.txt",
        "acceptance.json",
    }
    assert package["audio_sha256"] == accepted["progress"]["audio_acceptance"]["audio_sha256"]


class RejectedOnce:
    """The provider first refuses for lack of credit, then hears each part exactly."""

    def __init__(self, repo, project, run_id):
        self.repo, self.project, self.run_id, self.calls = repo, project, run_id, []

    def submit(self, request, body, credential):
        from podgen.audio.http import HttpReceipt

        self.calls.append(request)
        if len(self.calls) == 1:
            return HttpReceipt(402, {"content-type": "application/json"}, b'{"status":402}', 20)
        audio = AudioJournal(self.repo, self.project, uuid5(self.run_id, "audio-render/2"))
        parts = [" ".join(texts(cell["request"]["body"])).split() for cell in audio.get("manifest")["cells"]]
        words, segments = [], []
        for index, said in enumerate(parts):
            step = 3 / (len(said) + 1)
            segments += [
                dict(text=w, start=index * 3 + (i + 0.2) * step, end=index * 3 + (i + 0.7) * step)
                for i, w in enumerate(said)
            ]
            words += said
        value = dict(text=" ".join(words), duration=len(parts) * 3, segments=segments, request_id="fixture")
        return HttpReceipt(200, {"content-type": "application/json"}, json.dumps(value).encode(), 20)


def test_a_rejected_recognition_is_retried_once_without_new_synthesis(repo, scope, producer, monkeypatch):
    from podgen.episodes.production import audio as episode_audio
    from podgen.episodes.production import providers
    from podgen.episodes.production.contracts import AudioVerification
    from podgen.episodes.production.journal import ProductionJournal

    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    price = dict(usd_per_audio_minute="0.006", source_url="https://docs.fish.audio", checked_at="2026-11-23")
    settings = config.settings.model_copy(
        update=dict(audio_max_part_characters=256, audio_verification=AudioVerification(price=price))
    )
    config = config.model_copy(update=dict(settings=settings))
    runner.writer.provider_factory = lambda _: LongScriptProvider()
    monkeypatch.setenv("FISH_API_KEY", "fixture")
    recognizer = RejectedOnce(repo, scope[0], config.run_id)
    runner.recognition_transport = recognizer
    monkeypatch.setattr(providers, "recognition_transport", lambda settings: recognizer)
    prepare(repo, scope[0], config)
    with pytest.raises(ContractError, match="episode_audio_verification_provider_rejected"):
        runner.run()
    journal = ProductionJournal(repo, scope[0], config.run_id)
    assert journal.read()["progress"]["error_code"] == "episode_audio_verification_provider_rejected"
    synthesized = len(speech.calls)
    assert (
        episode_audio.retry_recognition(repo, scope[0], journal.read(), "Credit added.")
        == "candidates_generated"
    )
    assert runner.run()["status"] == "assembled"
    assert len(recognizer.calls) == 2 and len(speech.calls) == synthesized
    with pytest.raises(ContractError, match="episode_recognition_retry_requires_provider_rejection"):
        episode_audio.retry_recognition(repo, scope[0], journal.read(), "Again.")
