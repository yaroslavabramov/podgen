import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.audio.editing.localization import locate, unique_cuts
from podgen.audio.editing.timeline import sentences
from podgen.audio.recognition.contracts import Recognition as Recognized
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.local_replacement.context import compile_context
from tests.audio_cases import signal
from tests.audio_local_replacement_fixtures import Recognition


def test_repeated_sentence_identity_is_positional_and_missing_repeat_is_ambiguous():
    words = ["текст", "текст", "текст"]
    assert unique_cuts(words, words, [1, 2]) == [1, 2]
    with pytest.raises(ContractError, match="ambiguous"):
        unique_cuts(words, words[:-1], [1, 2])


def test_context_preserves_source_spacing_and_projects_frozen_cast():
    utterances = [
        dict(utterance_id=str(uuid4()), speaker="A", text="Один.  Два.\nТри. Четыре."),
        dict(utterance_id=str(uuid4()), speaker="B", text="Пять. Шесть."),
    ]
    source = dict(
        utterances=utterances,
        direction=dict(style="exact style"),
        speaker_voices={"A": "voice-a", "B": "voice-b"},
    )
    rows = sentences(utterances)
    short, selection = compile_context(uuid4(), source, [rows[1]], "ru")
    assert short.utterances[0].text == "Один.  Два.\nТри."
    assert short.speaker_voices == {"A": "voice-a"}
    assert short.direction.style == "exact style"
    assert selection["target_token_range"] == [1, 2]
    short, selection = compile_context(uuid4(), source, [rows[3]], "ru")
    assert [row.speaker for row in short.utterances] == ["A", "B"]
    assert short.speaker_voices == source["speaker_voices"]
    assert short.utterances[1].text == "Пять."


def recognized(utterances, seconds=4):
    return Recognized.model_validate(json.loads(Recognition(utterances, seconds).receipt.body))


def test_target_internal_mismatch_is_allowed_but_shared_boundary_segment_is_not():
    utterances = [
        dict(utterance_id=str(uuid4()), speaker="A", text="Левый сосед. Ошибка внутри. Правый сосед.")
    ]
    r = recognized(utterances)
    raw = r.model_dump(mode="json")
    raw["text"] = raw["text"].replace("Ошибка", "Изменение")
    raw["segments"][2]["text"] = "Изменение"
    result = locate(utterances, [2, 4], Recognized.model_validate(raw), signal(seconds=4))
    assert result["recognized_target_token_range"] == [2, 4]
    raw["segments"][1]["text"] += " " + raw["segments"][2]["text"]
    raw["segments"][1]["end"] = raw["segments"][2]["end"]
    del raw["segments"][2]
    with pytest.raises(ContractError, match="overlap_or_no_gap"):
        locate(utterances, [2, 4], Recognized.model_validate(raw), signal(seconds=4))


def test_collapsed_onset_cannot_be_cut_inside_group():
    utterances = [dict(utterance_id=str(uuid4()), speaker="A", text="Левый. У него.")]
    raw = recognized(utterances).model_dump(mode="json")
    raw["segments"][1]["start"] = raw["segments"][1]["end"] = raw["segments"][2]["start"]
    r = Recognized.model_validate(raw)
    result = locate(utterances, [0, 1], r, signal(seconds=4))
    assert result["collapsed_timing_groups"][0]["internal_word_times"] == "unavailable"
    with pytest.raises(ContractError, match="overlap_or_no_gap"):
        locate(utterances, [0, 2], r, signal(seconds=4))


def test_actual_snow_recognition_collapsed_u_has_observed_next_word_onset():
    value = json.loads((Path(__file__).parent / "fixtures/audio/snow-context-recognition.json").read_text())
    r = Recognized.model_validate(value["recognition"])
    result = locate(value["utterances"], value["target_token_range"], r, signal(seconds=r.duration))
    right = result["boundary_proofs"][1]
    assert right["gap_start_frame"] == round(12.88 * 24000)
    assert right["gap_end_frame"] == round(13.6 * 24000)
    assert right["gap_start_frame"] <= result["end_frame"] < right["gap_end_frame"]
    assert result["collapsed_timing_groups"]


def test_short_audible_insert_needs_finite_peak_but_not_integrated_loudness():
    import io
    import math
    import wave

    from podgen.audio.editing.splice import render

    audio = signal(seconds=0.1)
    rendered, transform = render(audio, (1, 2, 48000), dict(applied_gain_db=0, target_true_peak_dbtp=-1))
    assert rendered and math.isfinite(transform["true_peak_dbtp"])
    stream = io.BytesIO()
    with wave.open(stream, "wb") as out:
        out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        out.writeframes(bytes(4800))
    with pytest.raises(ContractError, match="no_finite_true_peak"):
        render(stream.getvalue(), (1, 2, 48000), dict(applied_gain_db=0, target_true_peak_dbtp=-1))
